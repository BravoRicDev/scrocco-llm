# scrocco-llm · gateway LLM OpenAI-compatible
# Le CREDENZIALI non stanno nell'immagine: var/ è bind-montata a runtime.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG UID=1001
ARG GID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=4001

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY docs/ docs/

# utente NON-root allineato all'owner dei file bind-montati (var/)
RUN groupadd -g ${GID} scrocco && useradd -m -u ${UID} -g ${GID} scrocco \
    && mkdir -p /app/var \
    && chown -R ${UID}:${GID} /app
USER ${UID}:${GID}

EXPOSE 4001

# LIVENESS, non salute del servizio: il check legge l'eta' del battito che
# l'event loop scrive ogni 5s (app/liveness.py), senza HTTP. Un gateway sotto
# carico e' LENTO ma vivo e resta healthy; unhealthy solo se il loop e' fermo
# da GATEWAY_HEARTBEAT_MAX_AGE (120s). Prima: /healthz via HTTP con timeout 3s
# -> sotto carico falliva, il container veniva riavviato e ripartiva freddo
# sotto lo stesso carico (crash-loop). /healthz resta per il monitoraggio.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "app.liveness"]

# Processi: `GATEWAY_WORKERS` (default 1). Con 1 `app.serve` fa exec dello
# STESSO comando di prima (`python -m uvicorn app.main:app --host --port`).
# Con N>1 (o `auto`) diventa un supervisore: una porta condivisa da N worker,
# richieste instradate per sessione al worker che ne tiene lo stato,
# osservazioni globali di routing (cooldown, EMA, finestre, in volo)
# replicate tra i worker, riavvio dei worker morti o bloccati. Vedi
# app/serve.py, app/cluster.py e docs/OPERATIONS.md ("Multi-worker").
CMD ["sh", "-c", "exec python -m app.serve --host \"${GATEWAY_HOST:-0.0.0.0}\" --port \"${GATEWAY_PORT:-4001}\""]
