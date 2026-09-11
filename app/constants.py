"""Costanti centralizzate per il gateway scrocco-llm.

Tutti i valori magic e configurazioni temporali sono definiti qui
per garantire unica fonte di verità e facilità di manutenzione.
"""

# Timing & TTL (secondi)
WATCH_INTERVAL_SECONDS = 5.0
JOB_TTL_SECONDS = 24 * 3600  # 24h
STICKY_TTL_SECONDS = 3600
COOLDOWN_BASE_SECONDS = 600
STATS_CLEANUP_THRESHOLD = 172800  # 48h (equivalente a un restart del processo)

# Token / Buffer
CHARS_PER_TOKEN = 4
PEEK_BUFFER_MAX_BYTES = 10 * 1024 * 1024  # 10MB

# Retry & Connection
DB_MAX_RETRIES = 3
DB_RETRY_BASE_DELAY = 100  # ms
DB_RETRY_MAX_DELAY = 2000  # ms
DB_POOL_MAX_SIZE = 20
DB_IDLE_TIMEOUT = 30000  # ms
DB_CONNECTION_TIMEOUT = 5000  # ms

# Gateway
DEFAULT_PORT = 3000
DEFAULT_GATEWAY_URL = "http://scrocco-llm:4001"
GATEWAY_TIMEOUT_MS = 10000

# Scoring Weights (Reputation System)
# Lower score is better. All values are configurable here.
SCORING_WEIGHTS = {
    "ATTEMPT_PROVIDER": 1,      # +1 per tentativo con stesso provider/modello (escluso sé stesso)
    "ATTEMPT_KEY": 1,           # +1 per tentativo con stessa chiave (escluso sé stesso)
    "FAIL_DEPLOYMENT": 5,       # +5 per fallimento specifico del deployment
    "FAIL_PROVIDER": 2,         # +2 per fallimento del provider/modello (tutti i deployment)
    "FAIL_KEY": 2,              # +2 per fallimento della chiave (tutti i deployment)
    "SUCCESS_DEPLOYMENT": -10,  # -10 per successo del deployment specifico
    "SUCCESS_PROVIDER": -2,     # -2 per successo del provider/modello (tutti i deployment)
    "SUCCESS_KEY": -2,          # -2 per successo della chiave (tutti i deployment)
}
