"""Logging del processo: console colorata e file sotto var/.

[IT] Due passi, nell'ordine in cui app/main.py li chiama:
- `install_console()`: root a INFO con l'handler colorato di
  app/terminal_logging.py (docker logs / terminale). INFO e' il presupposto
  della mappa di visibilita' dei log (tests/test_log_visibility.py): un
  `log.debug` non arriva mai in produzione.
- `install_file_logging(var_dir)`: var/gateway.log (tutto a INFO) e
  var/error-audit.log (solo i body upstream d'errore), con formato piatto.
  Multi-worker: ruota solo il leader.

I messaggi di questo modulo escono sul logger "nx.main", come prima.

[EN] Process logging: colored console at INFO + plain file handlers.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from . import cluster
from .terminal_logging import setup_colored_logging

log = logging.getLogger("nx.main")


def install_console() -> logging.Handler:
    """Root a INFO con l'handler colorato (sostituisce un basicConfig
    precedente: `force=True`)."""
    console_handler = setup_colored_logging()
    logging.basicConfig(level=logging.INFO, handlers=[console_handler], force=True)
    return console_handler


def install_file_logging(var_dir) -> None:
    """Handler su FILE oltre allo stdout (docker logs resta invariato).

    - var/gateway.log  : tutto il log INFO (bind-montato -> sopravvive al
      redeploy del container, dove lo stdout viene perso).
    - var/error-audit.log : SOLO i body upstream con "error" (logger
      nx.erroraudit, alimentato da forwarder.UpstreamError + le righe
      PASS-THROUGH). File LOCALE, gitignored (var/*), da rivedere ogni tanto.
    Fail-safe: se un path non e' scrivibile si prosegue col solo stdout.
    Saltato sotto pytest (PYTEST_CURRENT_TEST) per non sporcare il repo.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    from logging.handlers import RotatingFileHandler, WatchedFileHandler

    def _file_handler(path: str) -> logging.Handler:
        # Multi-worker: stessi file per tutti i processi, ma ruota SOLO il
        # leader; gli altri appendono e riaprono il file quando e' stato
        # ruotato (WatchedFileHandler), senza rinominarlo in parallelo.
        if cluster.enabled() and not cluster.is_leader():
            return WatchedFileHandler(path, encoding="utf-8")
        return RotatingFileHandler(path, maxBytes=mb * 1024 * 1024, backupCount=bk, encoding="utf-8")

    # Same format as console for consistency
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    mb = int(os.environ.get("GATEWAY_LOG_MAX_MB", "20"))
    bk = int(os.environ.get("GATEWAY_LOG_BACKUPS", "5"))
    main_path = os.environ.get("GATEWAY_LOG_FILE", str(Path(var_dir) / "gateway.log"))
    audit_path = os.environ.get("GATEWAY_ERROR_LOG_FILE", str(Path(var_dir) / "error-audit.log"))
    try:
        h = _file_handler(main_path)
        h.setFormatter(logging.Formatter(fmt))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    except OSError as exc:  # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s): solo stdout", main_path, exc)
    try:
        ah = _file_handler(audit_path)
        ah.setFormatter(logging.Formatter(fmt))
        ah.setLevel(logging.INFO)
        eaudit = logging.getLogger("nx.erroraudit")
        eaudit.addHandler(ah)
        eaudit.propagate = True  # va anche in gateway.log/stdout
    except OSError as exc:  # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s)", audit_path, exc)
