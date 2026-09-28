"""Risposte d'errore HTTP in formato OpenAI condivise dagli endpoint.

[IT] Prima vivevano in `app/main.py` e i moduli estratti le raggiungevano
con `import app.main as M` solo per costruire un 401/403: una dipendenza dal
modulo radice che non serviva.

[EN] Shared OpenAI-style 400/401/403 error responses.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse


def unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401, content={"error": {"message": detail, "type": "auth_error", "param": None, "code": "401"}}
    )


def forbidden(model: str, profile: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": f"Model '{model}' not allowed for this key" + (f" (profile '{profile}')" if profile else ""),
                "type": "permission_error",
                "param": None,
                "code": "403",
            }
        },
    )


def invalid_json_body() -> JSONResponse:
    """400 per un body che non e' JSON valido (endpoint OpenAI-compatibili)."""
    return JSONResponse(
        status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
    )
