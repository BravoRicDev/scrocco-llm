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


class UpstreamError(AppError):
    """Errore upstream (provider LLM)."""

    def __init__(self, status: int, detail: str):
        super().__init__(status, detail, "upstream_error", str(status))
