"""Troncamento cache-aware del contesto: dei tool output VECCHI resta l'inizio
e la fine, mai lo stub secco.

[IT] COSA: quando la cache del provider e' FREDDA (nessun detentore sano e
raggiungibile per la sessione), possiamo riscrivere il prefisso senza perdere
nulla: per i messaggi `role=="tool"` piu' vecchi della frontiera con contenuto
testuale oltre `max_tool_output_chars` sostituiamo il `content` con
    [tool output omesso: {n} caratteri]      <- riga template (stub_text)
    [nome-tool] {n} char, {righe} righe[, exit X]   <- summary tool-agnostic
    {head <= head_chars, taglio a fine riga}
    ...[omessi {k} char]...
    {tail <= tail_chars, taglio a inizio riga}
Doppioni identici (stesso output ripetuto) diventano un rimando cortissimo al
`tool_call_id` della copia piu' recente. Quando la cache e' CALDA NON si tocca
nulla (si preserva il prefisso byte-per-byte).

DETERMINISMO (cache-correct): lo stub e' funzione PURA del contenuto
originale: stesso input -> stessi byte, sempre. Una frontiera che avanza in
avanti (mono-tona: i messaggi nuovi non tornano mai indietro) non riscrive
cio' che era gia' stato compresso -> nessuna invalidazione ripetuta della
prompt-cache.

VINCOLI: non si rimuovono MAI messaggi (l'accoppiata assistant.tool_calls /
tool.tool_call_id resta valida); si tocca SOLO il `content` stringa dei tool
vecchi (mai assistant/user/system, mai liste multimodali, mai gli argomenti
dei tool_calls); gli output che CONTENGONO ERRORI (Traceback/...Error/
Exception/exit != 0) NON si toccano per nessuna ragione, nemmeno in
overflow; soglia `min_saved_tokens` per evitare churn inutile.

[EN] WHAT: cache-aware context trimming. When the provider cache is cold,
large OLD tool outputs are replaced (content-only) with a deterministic
summary line + head + omitted-middle marker + tail; identical duplicates
become a back-reference to the newest call. Error outputs are never touched.
"""

from __future__ import annotations

import hashlib
import logging
import re

log = logging.getLogger("nx.ctxcompact")

DEFAULT_STUB = "[tool output omesso: {n} caratteri]"

# Guardie di riconoscimento per l'idempotenza (mai ri-comprimere).
_RIMANDO_PREFIX = "[rimando:"
# Errori "importanti": mai toccare (decisone operatore: nemmeno in overflow).
_ERROR_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z]*Error|Exception|ENOSPC|EACCES|SIGKILL|Traceback)\b")
# Exit code nel testo del tool result (formati generici dei vari client).
_EXIT_RE = re.compile(r"(?im)\bexit(?:[ _-]*code)?\s*[:=]\s*(-?\d+)")
# Floor del walk a budget: gli ultimi N messaggi restano sempre integri.
_MIN_PROTECTED_MSGS = 8


class CtxCompactConfig:
    def __init__(self, enabled: bool = True, keep_turns: int = 4,
                 max_tool_output_chars: int = 2000,
                 min_saved_tokens: int = 500,
                 stub_text: str = DEFAULT_STUB,
                 min_ctx_tokens: int = 50000,
                 on_deployment_switch: bool = True,
                 switch_min_tokens: int = 8000,
                 abs_headroom_ratio: float = 0.8,
                 head_chars: int = 600,
                 tail_chars: int = 600,
                 keep_tail_pct: float = 2.0,
                 keep_error_outputs: bool = True):
        self.enabled = bool(enabled)
        self.keep_turns = int(keep_turns)
        self.max_tool_output_chars = int(max_tool_output_chars)
        self.min_saved_tokens = int(min_saved_tokens)
        self.stub_text = stub_text or DEFAULT_STUB
        self.min_ctx_tokens = int(min_ctx_tokens)
        self.on_deployment_switch = bool(on_deployment_switch)
        self.switch_min_tokens = int(switch_min_tokens)
        # Isteresi anti-churn: la soglia ASSOLUTA scatta solo se il contesto e'
        # entro questa frazione della finestra del deployment scelto (0 =
        # disabilitata, comportamento storico). overflow/switch non sono
        # filtrati.
        self.abs_headroom_ratio = max(0.0, float(abs_headroom_ratio))
        # Caratteri fissi di inizio/fine conservati nello stub (min 600 per
        # default, configurabili; head=tail=0 -> stub secco legacy).
        self.head_chars = int(head_chars)
        self.tail_chars = int(tail_chars)
        # Frontiera dinamica: protegge la coda finche' sta in
        # keep_tail_pct% della finestra del deployment (0 = disattivato, vale
        # solo keep_turns).
        self.keep_tail_pct = float(keep_tail_pct)
        self.keep_error_outputs = bool(keep_error_outputs)


def create_ctxcompact_config(policy_dict: dict | None = None) -> CtxCompactConfig:
    """Costruisce la config dal blocco `cache_aware.context_truncation`."""
    cfg = CtxCompactConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    ca = policy_dict.get("cache_aware") or {}
    if not isinstance(ca, dict):
        return cfg
    ct = ca.get("context_truncation")
    if ct is None:
        ct = ca                     # consenti forma piatta (retro-compat)
    if not isinstance(ct, dict):
        return cfg
    if "enabled" in ct:
        cfg.enabled = bool(ct["enabled"])
    for src, attr in (("keep_turns", "keep_turns"),
                      ("max_tool_output_chars", "max_tool_output_chars"),
                      ("min_saved_tokens", "min_saved_tokens"),
                      ("min_ctx_tokens", "min_ctx_tokens"),
                      ("switch_min_tokens", "switch_min_tokens"),
                      ("head_chars", "head_chars"),
                      ("tail_chars", "tail_chars")):
        if ct.get(src) is not None:
            setattr(cfg, attr, int(ct[src]))
    for src, attr in (("keep_tail_pct", "keep_tail_pct"),
                      ("abs_headroom_ratio", "abs_headroom_ratio")):
        if ct.get(src) is not None:
            setattr(cfg, attr, float(ct[src]))
    if "on_deployment_switch" in ct:
        cfg.on_deployment_switch = bool(ct["on_deployment_switch"])
    if "keep_error_outputs" in ct:
        cfg.keep_error_outputs = bool(ct["keep_error_outputs"])
    if ct.get("stub_text"):
        cfg.stub_text = str(ct["stub_text"])
    return cfg


def ctxcompact_config_from_policy(policy) -> CtxCompactConfig:
    """Costruisce la config dai campi gia' parsati in `Policy`."""
    if policy is None:
        return CtxCompactConfig()
    return CtxCompactConfig(
        enabled=bool(getattr(policy, "cache_ctx_truncation_enabled", True)),
        keep_turns=int(getattr(policy, "cache_ctx_keep_turns", 4) or 4),
        max_tool_output_chars=int(
            getattr(policy, "cache_ctx_max_tool_output_chars", 2000) or 2000),
        min_saved_tokens=int(
            getattr(policy, "cache_ctx_min_saved_tokens", 500) or 500),
        stub_text=str(getattr(policy, "cache_ctx_stub_text", DEFAULT_STUB)
                      or DEFAULT_STUB),
        min_ctx_tokens=int(
            getattr(policy, "cache_ctx_min_ctx_tokens", 50000) or 0),
        on_deployment_switch=bool(
            getattr(policy, "cache_ctx_on_deployment_switch", True)),
        switch_min_tokens=int(
            getattr(policy, "cache_ctx_switch_min_tokens", 8000) or 0),
        abs_headroom_ratio=float(
            getattr(policy, "cache_ctx_abs_headroom_ratio", 0.8) or 0.0),
        head_chars=int(getattr(policy, "cache_ctx_head_chars", 600) or 0),
        tail_chars=int(getattr(policy, "cache_ctx_tail_chars", 600) or 0),
        keep_tail_pct=float(getattr(policy, "cache_ctx_keep_tail_pct", 2.0)
                            or 0.0),
        keep_error_outputs=bool(
            getattr(policy, "cache_ctx_keep_error_outputs", True)),
    )


def should_compact(cfg: CtxCompactConfig, ctx_est: int, max_in: int = 0,
                   holder: str | None = None, dep_unique: str | None = None,
                   session_compact: bool = False,
                   same_family: bool = False) -> dict:
    """Decide se troncare e perche'. Ritorna un dict:
    {compact, reason (str), cold (bool), overflow (bool)}.

    Trigger (poi sticky a livello di sessione, gestito dal chiamante):
      - overflow: ctx_est > max_input del deployment scelto (max_in>0);
      - abs:      ctx_est >= min_ctx_tokens (soglia assoluta);
      - switch:   cache FREDDA (nessun detentore o detentore != deployment)
                  e ctx_est >= switch_min_tokens;
      - sticky:   la sessione era gia' compatta.

    `same_family=True` (deployment scelto e detentore appartengono alla stessa
    famiglia di modelli, anche su provider diversi) sopprime SOLO il trigger
    `switch`: la prompt-cache resta calda tra provider gemelli. `overflow`,
    `abs` e `sticky` restano invariati.
    """
    if not cfg.enabled:
        return {"compact": False, "reason": "", "cold": False,
                "overflow": False}
    cold = (holder is None) or (dep_unique is not None
                                and holder != dep_unique)
    overflow = max_in > 0 and ctx_est > max_in
    # Isteresi anti-churn: la soglia assoluta NON riscrive il prefisso finche'
    # la finestra del deployment scelto ha ampio margine (evita di invalidare
    # la prompt-cache per oscillazioni attorno a min_ctx_tokens). Con max_in
    # ignoto (0) o ratio<=0 vale il comportamento storico.
    near_saturation = True
    if max_in > 0 and cfg.abs_headroom_ratio > 0:
        near_saturation = ctx_est >= int(max_in * cfg.abs_headroom_ratio)
    reasons = []
    if overflow:
        reasons.append("overflow")
    if (cfg.min_ctx_tokens > 0 and ctx_est >= cfg.min_ctx_tokens
            and (overflow or near_saturation)):
        reasons.append("abs")
    if (cfg.on_deployment_switch and cold and not same_family
            and ctx_est >= cfg.switch_min_tokens):
        reasons.append("switch")
    if session_compact:
        reasons.append("sticky")
    return {"compact": bool(reasons), "reason": ",".join(reasons),
            "cold": cold, "overflow": overflow}


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content
                   if isinstance(p, dict))
    return 0


def _already_stub(content, cfg) -> bool:
    """Idempotenza: non ri-comprimere mai uno stub o un rimando."""
    if not isinstance(content, str):
        return False
    if content.startswith(cfg.stub_text.split("{n}", 1)[0]):
        return True
    if content.startswith(_RIMANDO_PREFIX):
        return True
    # stub ricchi prodotti con template diversi in passato
    return "[omessi " in content[:80] or " caratteri]\n[" in content[:120]


def _exit_code(text: str) -> int | None:
    m = _EXIT_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _has_error(text: str, exit_code: int | None) -> bool:
    if exit_code not in (None, 0):
        return True
    return bool(_ERROR_RE.search(text))


def _tool_labels(messages) -> dict:
    """tool_call_id -> nome funzione: SOLA etichetta per il summary, nessuna
    interpretazione degli argomenti (scrocco e' agnostico sui tool)."""
    labels: dict = {}
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            cid = tc.get("id")
            name = ((tc.get("function") or {}).get("name")) if isinstance(
                tc.get("function"), dict) else None
            if cid and name:
                labels[cid] = name
    return labels


def _cut_head(text: str, limit: int) -> str:
    """Primi `limit` char, tagliati all'ultimo fine riga completo."""
    head = text[:limit]
    nl = head.rfind("\n")
    return head[:nl + 1] if nl > 0 else head


def _cut_tail(text: str, limit: int) -> str:
    """Ultimi `limit` char, a partire da un inizio riga."""
    tail = text[-limit:]
    nl = tail.find("\n")
    return tail[nl + 1:] if nl != -1 and nl < len(tail) - 1 else tail


def _stub_for(content: str, name: str, cfg: CtxCompactConfig) -> str:
    """Stub deterministico: funzione PURA del contenuto originale."""
    n = len(content)
    if cfg.head_chars <= 0 and cfg.tail_chars <= 0:
        return cfg.stub_text.replace("{n}", str(n))
    lines = content.count("\n") + 1
    code = _exit_code(content)
    summary = f"[{name}] {n:,} char, {lines} righe"
    if code is not None:
        summary += f", exit {code}"
    head = _cut_head(content, cfg.head_chars) if cfg.head_chars > 0 else ""
    tail = _cut_tail(content, cfg.tail_chars) if cfg.tail_chars > 0 else ""
    omitted = max(n - len(head) - len(tail), 0)
    marker = f"\n...[omessi {omitted:,} char]...\n"
    return (f"{cfg.stub_text.replace('{n}', str(n))}\n"
            f"{summary}\n{head}{marker}{tail}")


def _msg_tokens(m) -> int:
    """Stima rough (chars/4 + overhead, include l'envelope dei tool_calls) per
    il walk della frontiera a budget."""
    n = _content_len(m.get("content")) if isinstance(m, dict) else 0
    tcs = m.get("tool_calls") if isinstance(m, dict) else None
    if tcs:
        n += len(str(tcs))
    return n // 4 + 10


def _walk_boundary(messages, max_in: int, keep_tail_pct: float) -> int:
    """Frontiera a BUDGET TOKEN (stile Hermes, adattata): protegge la coda
    finche' resta dentro keep_tail_pct% della finestra; floor fisso di
    _MIN_PROTECTED_MSGS messaggi integri. Ritorna il primo indice protetto
    (0 = budget generoso, nessun vincolo in piu')."""
    if keep_tail_pct <= 0 or max_in <= 0 or not messages:
        return 0
    budget = int(max_in * keep_tail_pct / 100.0)
    n = len(messages)
    min_protect = min(_MIN_PROTECTED_MSGS, n)
    accum = 0
    boundary = 0
    for i in range(n - 1, -1, -1):
        t = _msg_tokens(messages[i])
        if accum + t > budget and (n - i) >= min_protect:
            boundary = i
            break
        accum += t
        boundary = i
    return boundary


def compact_tool_outputs(messages, cfg: CtxCompactConfig, max_in: int = 0,
                         estimator=None):
    """Ritorna (nuova_lista, report). Non muta l'input.

    report: {stubbed, deduped, saved_chars, saved_tokens_est, boundary,
    changed}. Se il risparmio stimato < min_saved_tokens la lista originale e'
    ritornata invariata (changed=False) per non alterare la cache per nulla.

    `max_in` (max_input_tokens del deployment scelto) + `keep_tail_pct`
    attivano la frontiera dinamica per budget token; `estimator` (callable
    lista->token, es. router.estimate_tokens) sostituisce l'euristica //4 per
    la soglia min_saved_tokens.
    """
    rep = {"stubbed": 0, "deduped": 0, "saved_chars": 0,
           "saved_tokens_est": 0, "boundary": None, "changed": False}
    if not cfg.enabled or not messages:
        return messages, rep

    user_idx = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "user"]
    if not user_idx:
        return messages, rep               # niente turni utente: non toccare
    keep_n = max(0, cfg.keep_turns)
    if keep_n <= 0:
        boundary = len(messages)
    else:
        boundary = user_idx[-keep_n] if len(user_idx) >= keep_n else user_idx[0]
    # Frontiera dinamica: il budget token puo' stringere DENTRO i keep_turns
    # (finestre enormi: l'ultimo turno da solo puo' valere piu' del budget),
    # mai allargarle oltre se il budget e' generoso: max() delle due.
    boundary = max(boundary, _walk_boundary(messages, max_in,
                                            cfg.keep_tail_pct))
    rep["boundary"] = boundary

    labels = _tool_labels(messages)
    new = list(messages)
    saved = 0
    stubbed = 0
    deduped = 0
    changed_msgs: list = []                # (orig, nuova) per l'estimator

    def _cost(msg_list, plain_chars):
        if estimator is None:
            return plain_chars
        return estimator(msg_list)

    # --- PASS 1: dedup (rimando al tool_call_id della copia piu' recente) ---
    newest: dict = {}                      # md5[:12] -> tool_call_id
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "tool"
                and i < boundary):
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) < cfg.max_tool_output_chars:
            continue
        if _already_stub(content, cfg):
            continue
        code = _exit_code(content)
        if cfg.keep_error_outputs and _has_error(content, code):
            continue                       # duplicato d'errore: non toccare
        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        cid = m.get("tool_call_id")
        if h in newest:
            stub = f"{_RIMANDO_PREFIX} output identico al tool_call_id {newest[h]}, {len(content):,} char]"
            new[i] = {**m, "content": stub}
            deduped += 1
            saved += len(content) - len(stub)
            changed_msgs.append((m, new[i]))
        else:
            newest[h] = cid or f"msg@{i}"
    rep["deduped"] = deduped

    # --- PASS 2: stub head+tail dei tool grandi (puliti, non rimandati) -----
    for i in range(len(messages)):
        m = new[i]                          # POST-pass1: non calpestare i rimandi
        if not (isinstance(m, dict) and m.get("role") == "tool" and i < boundary):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue                       # liste multimodali: intatte
        n = len(content)
        if n <= cfg.max_tool_output_chars:
            continue                       # output gia' piccolo: lascialo
        if _already_stub(content, cfg):
            continue                       # idempotenza
        code = _exit_code(content)
        if cfg.keep_error_outputs and _has_error(content, code):
            continue                       # errori MAI toccati (anche overflow)
        name = labels.get(m.get("tool_call_id"), "tool")
        stub = _stub_for(content, name, cfg)
        new[i] = {**m, "content": stub}
        stubbed += 1
        saved += n - len(stub)
        changed_msgs.append((m, new[i]))
    rep["stubbed"] = stubbed
    rep["saved_chars"] = saved
    if estimator is not None and changed_msgs:
        before = sum(_cost([o], 0) for o, _n in changed_msgs)
        after = sum(_cost([x], 0) for _o, x in changed_msgs)
        rep["saved_tokens_est"] = max(0, before - after)
    else:
        rep["saved_tokens_est"] = saved // 4
    if (stubbed == 0 and deduped == 0) or rep["saved_tokens_est"] < cfg.min_saved_tokens:
        return messages, {**rep, "changed": False}
    rep["changed"] = True
    return new, rep
