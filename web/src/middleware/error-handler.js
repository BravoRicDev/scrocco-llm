 /** Middleware di gestione errori unificato per Express.

 Garantisce che tutte le risposte di errore seguano lo stesso formato
 JSON, sia per le API che per le pagine web.
 */

import config from '../config.js';
import { logger } from '../services/logger.js';

/**
 * Gestisce un errore API restituendo una risposta JSON standardizzata.
 * Mantiene compatibilità esatta con il formato precedente.
 */
export function handleApiError(res, err) {
    const status = err.status || 500;
    const type = err.errorType || err.name || "server_error";
    const code = err.code || String(status);
    const message = err.message || "errore interno";

    // Log strutturato dell'errore (mai al client in produzione)
    logger.error(`[API Error] ${status} ${type}: ${message}`, err);

    return res.status(status).json({
        error: { message, type, code }
    });
}

/**
 * Gestisce un errore di pagina web restituendo un errore render.
 */
export function handleWebError(res, err) {
    const msg = config.nodeEnv === 'production'
        ? 'Errore interno'
        : (err?.message || 'Errore interno');

    logger.error(`[Web Error] ${err?.status || 500}: ${msg}`, err);
    return res.status(err?.status || 500).render('error', { message: msg });
}

/**
 * Middleware di errore finale per Express.
 */
export function errorHandler(err, req, res, _next) {
    // Gestione specifica errori PostgreSQL 22P02
    if (err?.code === '22P02') {
        if (req.path.startsWith('/api')) {
            return res.status(404).json({ error: { message: 'non trovato', type: 'invalid_request_error', code: '404' } });
        }
        return res.status(404).render('error', { message: 'Non trovato' });
    }

    // Distinzione API vs Web
    if (req.path.startsWith('/api')) {
        return handleApiError(res, err);
    }
    return handleWebError(res, err);
}
