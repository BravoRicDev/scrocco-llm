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

# UN solo processo, UN event loop, di proposito: cooldown, reputazione,
# coalescing, finestre di uso, richieste in volo e probe vivono in memoria
# (app/state.py). N worker = N copie indipendenti dello stato di routing (una
# chiave in cooldown sul worker 1 ripescata dal worker 2): prima va
# esternalizzato lo stato. La protezione dal sovraccarico e' la porta di
# ammissione (app/admission.py).
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${GATEWAY_HOST:-0.0.0.0}\" --port \"${GATEWAY_PORT:-4001}\""]
