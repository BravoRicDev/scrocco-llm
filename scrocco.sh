#!/usr/bin/env bash
# scrocco-llm · launcher interfaccia terminale (TUI Textual o menu lite)
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python

if [ "$1" = "--cli" ]; then
    exec "$PY" -m tui.cli_lite "${@:2}"
fi

# La TUI ha bisogno di TUTTE le dipendenze in requirements-tui.txt (textual
# per l'interfaccia, httpx per il client del gateway): se manca anche solo
# una, `tui.app` crasha all'avvio -> meglio ripiegare sulla versione lite.
have_textual() {
    [ -x "$PY" ] && "$PY" -c "import textual, httpx" >/dev/null 2>&1
}

# Nessuna venv? Provala a creare (serve python3-venv sul sistema).
ensure_venv() {
    [ -x "$PY" ] && return 0
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -m venv .venv >/dev/null 2>&1 || return 1
    [ -x "$PY" ]
}

# Installazione automatica opzionale (SCROCCO_TUI_AUTOINSTALL=1).
if ! have_textual && [ "${SCROCCO_TUI_AUTOINSTALL:-0}" = "1" ]; then
    ensure_venv
    echo "[scrocco-llm] installo le dipendenze della TUI (requirements-tui.txt)..."
    "$PY" -m pip install -r requirements-tui.txt >/dev/null 2>&1 \
        || "$PY" -m pip install --user -r requirements-tui.txt >/dev/null 2>&1 \
        || echo "[scrocco-llm] installazione automatica non riuscita, procedo in modalita lite"
fi

if have_textual; then
    exec "$PY" -m tui.app "$@"
fi

cat <<'EOF'
[Textual non installato nella venv]

Installazione consigliata (una tantum):

    python3 -m venv .venv
    .venv/bin/pip install -r requirements-tui.txt

Oppure in automatico al prossimo avvio (crea la venv se manca):

    SCROCCO_TUI_AUTOINSTALL=1 ./scrocco.sh

oppure usa la versione lite senza dipendenze extra:

    ./scrocco.sh --cli
EOF
exit 2
