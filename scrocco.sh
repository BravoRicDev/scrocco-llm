#!/usr/bin/env bash
# scrocco-llm · launcher interfaccia terminale (TUI Textual o menu lite)
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python

if [ "$1" = "--cli" ]; then
    exec "$PY" -m tui.cli_lite "${@:2}"
fi

if [ -x "$PY" ] && "$PY" -c "import textual" >/dev/null 2>&1; then
    exec "$PY" -m tui.app "$@"
fi

cat <<'EOF'
[Textual non installato nella venv]

Installazione consigliata (una tantum):

    .venv/bin/pip install textual

oppure usa la versione lite senza dipendenze extra:

    ./scrocco.sh --cli
EOF
exit 2
