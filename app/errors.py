"""Classi di errore standardizzate per il gateway.

Tutte le eccezioni custom ereditano da AppError per garantire
una gestione unificata e consistente across l'applicazione.
"""


class AppError(Exception):
    """Eccezione base per il gateway."""

    def __init__(self, status: int, message: str,
                 error_type: str = "server_error", code: str = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type
        self.code = code or str(status)


class UnauthorizedError(AppError):
    """Errore 401: Autenticazione richiesta o fallita."""

    def __init__(self, message="Autenticazione richiesta"):
        super().__init__(401, message, "auth_error", "unauthorized")


class NotFoundError(AppError):
    """Errore 404: Risorsa non trovata."""

    def __init__(self, message="Risorsa non trovata"):
        super().__init__(404, message, "invalid_request_error", "not_found")


class ForbiddenError(AppError):
    """Errore 403: Permesso negato."""

    def __init__(self, message="Permesso negato"):
        super().__init__(403, message, "permission_error", "forbidden")


class UpstreamError(AppError):
    """Errore upstream (provider LLM)."""

    def __init__(self, status: int, detail: str):
        super().__init__(status, detail, "upstream_error", str(status))
