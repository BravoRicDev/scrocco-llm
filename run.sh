#!/usr/bin/env bash
# scrocco-llm · avvio del backend su 127.0.0.1:4001
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    echo "[scrocco-llm] venv mancante: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

set -a; [ -f .env.gateway ] && . ./.env.gateway; set +a

# GATEWAY_WORKERS>1 (o auto): supervisore multi-worker (vedi app/serve.py)
if [ "${GATEWAY_WORKERS:-1}" != "1" ]; then
    exec .venv/bin/python -m app.serve \
        --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4001}"
fi

exec .venv/bin/python -m uvicorn app.main:app \
    --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4001}" "$@"
