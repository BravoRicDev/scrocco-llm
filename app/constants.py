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
