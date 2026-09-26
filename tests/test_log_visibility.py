"""Contratto di VISIBILITA' dei log: nessun errore deve restare muto.

Motivazione (perche' questi test esistono)
-----------------------------------------
In produzione il root logger e l'handler su file sono a INFO
(`app/main.py`: `basicConfig(level=INFO)` e `h.setLevel(INFO)` dentro
`_install_file_logging`). Quindi **ogni `log.debug` non finisce MAI in
`var/gateway.log`**. Storicamente 26 call site che descrivevano ERRORI
(perdita dati, save falliti, tick abortiti) erano a `debug`: erano
completamente invisibili in produzione.

Questo file e' la **rete di sicurezza** della promozione dei livelli:

- i test di gruppo (persistenza/recuperabili/movimento) impediscono che un
  fix di visibilita' venga perso in un refactor successivo;
- `test_verbose_restano_debug` e' la guardia **contro l'over-promozione**:
  se qualcuno alza i log verbosi a INFO il file `gateway.log` esplode e i
  test di massa falliscono per il rumore.

I test analizzano il SORGENTE con `ast` e agganciano i call site per
**testo del messaggio**, NON per numero di riga: cosi' il contratto
sopravvive a shift di righe, refactor e spostamenti di blocchi.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Levels numerically comparable (logging.DEBUG < INFO < WARNING < ERROR).
_DEBUG, _INFO, _WARNING, _ERROR = (
    logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR,
)

# Solo questi receiver sono considerati: sono i logger di modulo del progetto.
_LOGGERS = frozenset({"log", "_log", "LOG", "logger"})

# Keyword che dicono "qui c'e' stato un fallimento".
_FAILURE_WORDS = ("errore", "error", "fallit", "fallito", "fallita",
                  "fail", "exception", "traceback")

_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical"})


# --------------------------------------------------------------- helpers --
def _message_text(call: ast.Call) -> str | None:
    """Testo del primo argomento, se e' una stringa (costante o f-string
    senza campi variabili). None se non ricostruibile staticamente."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        # solo i pezzi costanti: un f-string con variabili non e' ancorabile
        return "".join(v.value for v in first.values
                       if isinstance(v, ast.Constant)) or None
    return None


def _has_exc_info(call: ast.Call) -> bool:
    return any(k.arg == "exc_info"
               and isinstance(k.value, ast.Constant)
               and k.value.value is True
               for k in call.keywords)


def _log_calls(relpath: str) -> list[dict]:
    """Tutte le chiamate di logging del file, con metadati utili."""
    path = APP_DIR / relpath
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id not in _LOGGERS:
            continue
        out.append({
            "file": relpath,
            "lineno": node.lineno,
            "level": func.attr,
            "levelno": getattr(logging, func.attr.upper()),
            "msg": _message_text(node),
            "exc_info": _has_exc_info(node),
        })
    return out


def _describe(call: dict) -> str:
    return f"{call['file']}:{call['lineno']} {call['level']}({call['msg']!r})"


def _find(relpath: str, marker: str) -> list[dict]:
    """Call site il cui messaggio contiene `marker` (substring stabile)."""
    hits = [c for c in _log_calls(relpath)
            if c["msg"] and marker in c["msg"]]
    assert hits, (f"nessun call site di log contiene {marker!r} in {relpath!r}: "
                  f"il contratto e' rotto (stringa spostata/rinominata?)")
    return hits


def _find_one(relpath: str, marker: str) -> dict:
    hits = _find(relpath, marker)
    assert len(hits) == 1, (
        f"attesi esattamente 1 call site per {marker!r} in {relpath!r}, "
        f"trovati {len(hits)}: {[_describe(h) for h in hits]}")
    return hits[0]


# =========================================================== 1. nessun
#    log.debug che descrive un errore (salvo eccezioni documentate).
#
#    Le eccezioni sono VOLUTE e documentate: sono i log che descrivono un
#    fallimento ma devono restare muti perche' sono per-corso (una riga per
#    ogni fallimento upstream) e/o il log "di stato" e' gia' emesso a
#    livello superiore dal ramo fratello.
_DEBUG_ERROR_EXEMPT = {
    # 1 riga PER fallimento upstream, su un path ad altissima frequenza. Il
    # fallimento vero e' gia' emesso a WARNING/ERROR dai rami `[fallback]`,
    # `[rep-attempt]`, `[summary]`: qui si annota solo il delta di punteggio.
    ("router.py", "[rep-fail]"),
    # Escluso esplicitamente dalla mappa: il volume non e' ancora stato
    # misurato in produzione (vedi output del refactor). Handler `AppError`:
    # ogni 4xx/409 di API del cliente arriva qui.
    ("main.py", "[error-handler]"),
}


def test_nessun_debug_descrive_un_errore():
    """Nessun `log.debug` in app/ deve descrivere un fallimento.

    Regola: se il testo contiene una keyword di fallimento (oppure la
    chiamata passa `exc_info=True`, cioe' stampa un traceback) il call site
    deve stare a INFO o sopra. I `debug` sono invisibili a INFO, quindi un
    errore "muto" in produzione.
    """
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = str(path.relative_to(APP_DIR))
        for call in _log_calls(rel):
            if call["levelno"] != _DEBUG:
                continue
            msg = call["msg"]
            if msg is None:
                # non ancorabile staticamente: non lo giudichiamo, ma non
                # e' nemmeno un caso che il contratto possa coprire.
                continue
            low = msg.lower()
            words = [w for w in _FAILURE_WORDS if w in low]
            if not (call["exc_info"] or words):
                continue
            if (rel, msg.split("%")[0].strip()) in _DEBUG_ERROR_EXEMPT \
                    or (rel, msg) in _DEBUG_ERROR_EXEMPT:
                continue
            offenders.append(
                f"{_describe(call)} "
                f"[{'exc_info=True' if call['exc_info'] else ''}"
                f"{' keyword=' + ','.join(words) if words else ''}]")
    assert not offenders, (
        "log.debug che descrive un errore (invisibili a INFO, in "
        f"produzione): {len(offenders)}\n  " + "\n  ".join(offenders))


# ================================================== 2. persistenza = error
# Perdita dati: record/buffer/stato non scritti a disco.
_PERSIST_ERROR = [
    ("ledger.py", "[ledger] record error"),
    ("ledger.py", "[ledger] flush_async error"),
    ("ledger.py", "[ledger] rotate error"),
    ("repairlog.py", "[repair] note error"),
    ("repairlog.py", "[repair] flush error"),
    ("repairlog.py", "[repair] rotate error"),
    ("atomic_store.py", "[atomic] save %s fallito"),
    ("main.py", "[stats] save fallito"),
    ("main.py", "[cooldown] save fallito"),
    ("main.py", "[warmstart] save fallito"),
]


@pytest.mark.parametrize("relpath,marker", _PERSIST_ERROR,
                         ids=[m for _, m in _PERSIST_ERROR])
def test_persistenza_usa_error(relpath, marker):
    """I call site di persistenza sono a ERROR (perdita dati, mai silenziosa)."""
    call = _find_one(relpath, marker)
    assert call["levelno"] >= _ERROR, (
        f"{_describe(call)}: un save/flush/rotate fallito e' perdita dati, "
        f"deve essere ERROR (o critico), non {call['level']}")


@pytest.mark.parametrize("relpath,marker", _PERSIST_ERROR[:7],
                         ids=[m for _, m in _PERSIST_ERROR[:7]])
def test_persistenza_ha_traceback(relpath, marker):
    """I call site dentro un `except` portano il traceback.

    Nota: i tre marker `main.py` (`stats`/`cooldown`/`warmstart`) NON
    sono in questa lista perche' stanno FUORI da un `except`: `save_json`
    non solleva (cattura internamente e ritorna False) e il traceback vero
    e' gia' emesso, con exc_info, da `atomic_store`. Aggiungere exc_info
    laggi' produrrebbe solo un fuorviante "NoneType: None".
    """
    call = _find_one(relpath, marker)
    assert call["exc_info"], (
        f"{_describe(call)}: dentro l'except manca exc_info=True, "
        f"senza traceback l'errore e' muto in produzione")


# ================================================= 3. recuperabili = warning
# Condizioni anomale ma recuperabili: il gateway continua a servire.
_RECOVERABLE_WARNING = [
    ("main.py", "[keyhealth] tick error"),
    ("main.py", "[images] sweep error"),
    ("main.py", "[thought_sig] save fallito"),
    ("main.py", "[images] mirror %s fallito: %s"),
    ("forwarder.py", "[max-input] note_discovered fallito per %s"),
    ("forwarder.py", "[strike] hook error: %s"),
    ("router.py", "[lifecycle] retire %s fallito"),
    ("router.py", "[probe] auto-retirement di %s fallito"),
    ("routing/warm.py", "[warm] note_warm_owner %s fallito"),
    ("autoprobe.py", "[autoprobe] giro ritirati terminato con errore"),
    ("autoprobe.py", "[autoprobe] pass terminato con errore"),
    ("autoprobe.py", "[hotreload] pass terminato con errore"),
    ("autoprobe.py", "[autoprobe] nightly %s: errore nel pass"),
]


@pytest.mark.parametrize("relpath,marker", _RECOVERABLE_WARNING,
                         ids=[m for _, m in _RECOVERABLE_WARNING])
def test_recuperabili_usa_warning(relpath, marker):
    """Gli errori recuperabili sono WARNING: visibili senza alarmare."""
    for call in _find(relpath, marker):
        assert call["levelno"] == _WARNING, (
            f"{_describe(call)}: errore recuperabile -> WARNING atteso, "
            f"trovato {call['level']}")


# ================================================== 4. movimento = info
# Bassa frequenza e/o spiega una decisione di routing ("perche' succede
# questo"), con un fratello gia' a info a poche righe di distanza.
_MOVEMENT_INFO = [
    ("main.py", "[refill] nessuna sveglia 429 matura"),
    ("main.py", "[sveglia] nessun dormiente maturo"),
    ("router.py", "[ladder] %s cronico (fail_24h>=%d)"),
    ("router.py", "[restrict] %s: failover same-model -> %s"),
    ("router.py", "[cooldown-class] %s classe=transient"),
    ("router.py", "[cooldown-class] %s classe=quota"),
]


@pytest.mark.parametrize("relpath,marker", _MOVEMENT_INFO,
                         ids=[m for _, m in _MOVEMENT_INFO])
def test_movimento_usa_info(relpath, marker):
    """I log di movimento/fallback sono INFO (volume basso, spiegano il routing)."""
    for call in _find(relpath, marker):
        assert call["levelno"] == _INFO, (
            f"{_describe(call)}: movimento di routing -> INFO atteso, "
            f"trovato {call['level']}")


def test_movimento_nessun_sopravvivenza_a_debug():
    """Nessun marker di movimento resta a debug (o sopra info per sbaglio)."""
    for relpath, marker in _MOVEMENT_INFO:
        for call in _find(relpath, marker):
            assert call["levelno"] == _INFO, _describe(call)


# ================================ 5. i verbose RESTANO debug (anti over-promozione)
# Se questi salgono a INFO il `gateway.log` diventa illeggibile. Sono il
# perno piu' importante di questo file.
_MUST_STAY_DEBUG = [
    # POST upstream: 1 riga per ogni richiesta, contiene header/payload shape
    ("forwarder.py", "[upstream] %s POST"),
    # 1 riga per ogni HTTP in ingresso
    ("observability.py", "[http] ->"),
    # ~48 righe per richiesta video (poll di attesa)
    ("main.py", "[video-wait]"),
    # pattern pinnato: la prima volta INFO, le ripetizioni DEBUG
    # (cfr. tests/test_logging_detail.py::test_defer_info_first_then_debug)
    ("router.py", "[defer] %s: scartati %d multimodali"),
]


@pytest.mark.parametrize("relpath,marker", _MUST_STAY_DEBUG,
                         ids=[m for _, m in _MUST_STAY_DEBUG])
def test_verbose_restano_debug(relpath, marker):
    """I log verbosi devono restare a DEBUG.

    E' la guardia contro l'over-promozione: questi call site sono 1 riga per
    richiesta (o per chunk SSE). Alzarli a INFO farebbe esplodere
    `var/gateway.log` e renderebbe il file illeggibile in produzione.
    """
    for call in _find(relpath, marker):
        assert call["levelno"] == _DEBUG, (
            f"{_describe(call)}: log verboso, deve restare DEBUG (non "
            f"{call['level']}): riporta il volume e valuta con misure reali")


def test_defer_info_prima_e_debug_dopo():
    """`[defer]`: la PRIMA volta INFO, le ripetizioni DEBUG.

    Copia di contratto del test esistente
    `tests/test_logging_detail.py::test_defer_info_first_then_debug`:
    quello verifica il comportamento a runtime, questo verifica che i due
    call site (rami if/else) non siano stati promossi entrambi.
    """
    calls = _find("router.py", "[defer] %s")
    # i due rami if/else del pattern: si distinguono per il testo ESATTO
    # (gli altri due [defer] sono "quota protetta, ..." e "riapertura").
    first = [c for c in calls if c["msg"].endswith(
        "quota protetta, scartati %d multimodali (%d text-only attivi)")]
    repeats = [c for c in calls if c["msg"] == "[defer] %s: scartati %d multimodali"]
    assert len(first) == 1, f"ramo INFO di [defer] non trovato/unico: {first}"
    assert len(repeats) == 1, f"ramo DEBUG di [defer] non trovato/unico: {repeats}"
    assert first[0]["levelno"] == _INFO, _describe(first[0])
    assert repeats[0]["levelno"] == _DEBUG, _describe(repeats[0])


# ============================== 6. la configurazione di logging non e' cambiata
def test_config_logger_default():
    """Root logger e handler su file restano a INFO.

    Il default INFO e' il presupposto di tutta la mappa: e' cioe' la
    ragione per cui un `debug` e' invisibile in `var/gateway.log`. Se un
    refiasse abbassasse root a DEBUG il file si riempirebbe di rumore; se lo
    alzasse a WARNING sparirebbero gli INFO (inclusi quelli promossi qui).
    """
    src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src, "app/main.py")
    assert "level=logging.INFO" in src, (
        "root logger non piu' a INFO: cambia la visibilita' di TUTTO il log")
    # l'handler su file deve restare a INFO
    tree = ast.parse(src, "app/main.py")
    file_levels = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setLevel"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "INFO"
    ]
    assert file_levels, "handler su file non piu' a INFO"
    # e deve stare dentro _install_file_logging (non altrove)
    install = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_install_file_logging")
    inner = [n for n in ast.walk(install)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "setLevel"]
    assert inner, "RotatingFileHandler senza setLevel esplicito"
