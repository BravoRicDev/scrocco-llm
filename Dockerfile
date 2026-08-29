# scrocco-llm · gateway LLM OpenAI-compatible
# Le CREDENZIALI non stanno nell'immagine: var/ è bind-montata a runtime.
FROM python:3.12-slim

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;port=os.environ.get('GATEWAY_PORT','4001');r=urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=3);raise SystemExit(0 if r.status==200 else 1)"]

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${GATEWAY_HOST:-0.0.0.0}\" --port \"${GATEWAY_PORT:-4001}\""]
