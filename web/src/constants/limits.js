 /** Costanti di limiti e configurazione per il web panel.

 Tutti i valori magic e configurazioni temporali sono definiti qui
 per garantire unica fonte di verità e facilità di manutenzione.
 */

// Timing & TTL (millisecondi)
export const DB_MAX_RETRIES = 3;
export const DB_RETRY_BASE_DELAY = 100; // ms
export const DB_RETRY_MAX_DELAY = 2000; // ms
export const DB_IDLE_TIMEOUT = 30000; // ms
export const DB_CONNECTION_TIMEOUT = 5000; // ms

// Buffer & Limits
export const PEEK_BUFFER_MAX = 10 * 1024 * 1024; // 10MB
export const TRUNCATE_LEN = 300;

// Gateway
export const GATEWAY_TIMEOUT_DEFAULT = 10000; // ms
export const MAX_REQUEST_BODY_SIZE = '50mb';
export const REQUEST_RATE_LIMIT_MAX = 10;
export const REQUEST_RATE_LIMIT_WINDOW = 15 * 60 * 1000; // 15 min

// Sessions
export const STICKY_TTL_SECONDS = 3600;
export const SESSION_COOKIE_NAME = 'token';
