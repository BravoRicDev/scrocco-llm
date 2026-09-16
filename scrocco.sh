#!/usr/bin/env bash
# scrocco-llm · launcher interfaccia terminale (TUI Textual o menu lite)
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python

if [ "$1" = "--cli" ]; then
    exec "$PY" -m tui.cli_lite "${@:2}"
fi

have_textual() {
    [ -x "$PY" ] && "$PY" -c "import textual" >/dev/null 2>&1
}

# Installazione automatica opzionale (SCROCCO_TUI_AUTOINSTALL=1).
if ! have_textual && [ "${SCROCCO_TUI_AUTOINSTALL:-0}" = "1" ]; then
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

    .venv/bin/python -m pip install -r requirements-tui.txt
    # oppure, senza pip nella venv:
    python3 -m pip install --target .venv/lib/python3.*/site-packages -r requirements-tui.txt

Per installarlo automaticamente al prossimo avvio:

    SCROCCO_TUI_AUTOINSTALL=1 ./scrocco.sh

oppure usa la versione lite senza dipendenze extra:

    ./scrocco.sh --cli
EOF
exit 2
