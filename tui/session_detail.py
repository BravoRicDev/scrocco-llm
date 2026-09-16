"""Dettaglio di UNA sessione (modal screen).

Consuma GET /admin/sessions/{session_id}: classifica dei deployment che hanno
partecipato (con successo) alla sessione, modello preferito, totali token e
stato runtime (sticky, cache holder, dep-guard, slow).
"""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from .gateway_client import GatewayClient, GatewayError


def _fmt_age(sec) -> str:
    try:
        d = float(sec or 0)
    except (TypeError, ValueError):
        return "-"
    if d < 60:
        return f"{int(d)}s"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    return f"{int(d // 86400)}g"


def _human(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class SessionDetailScreen(ModalScreen):
    """Classifica dei deployment che hanno servito con successo una sessione."""

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("q", "close", "Chiudi"),
        Binding("r", "refresh", "Ricarica"),
    ]

    def __init__(self, client: GatewayClient, session_id: str,
                 window: str = "7d"):
        super().__init__()
        self.client = client
        self.session_id = session_id
        self.window = window
        self._loading = False

    def compose(self):
        with Vertical(id="sess-detail-box"):
            yield Label("", id="sess-detail-title")
            yield Static("", id="sess-detail-summary")
            yield Label("[b]Deployment che hanno partecipato (ok in alto)[/]",
                        id="sess-detail-sub")
            yield DataTable(id="sess-detail-t", zebra_stripes=True,
                            cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#sess-detail-t", DataTable)
        for label, key in [
            ("deployment", "dep"), ("modello", "model"), ("chiamate", "calls"),
            ("ok", "ok"), ("fail", "fail"), ("success%", "sr"),
            ("prompt", "pt"), ("completion", "ct"), ("tot tok", "tt"),
            ("cache tok", "cache"), ("avg ms", "avg"), ("fb", "fb"),
            ("qc", "qc"), ("wd", "wd"),
        ]:
            t.add_column(label, key=key)
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.session_detail(self.session_id,
                                                        self.window)
            except GatewayError as e:
                self.query_one("#sess-detail-title", Label).update(
                    f"[red]errore sessione: {e}[/]")
                return

            totals = data.get("totals", {}) or {}
            pref = data.get("preferred_model") or {}
            self.query_one("#sess-detail-title", Label).update(
                f"[b cyan]SESSIONE[/] [b]{self.session_id}[/]  "
                f"[dim]finestra {self.window} · r ricarica · esc chiudi[/]"
            )
            self.query_one("#sess-detail-summary", Static).update(
                f"[b]Modello preferito:[/] [green]"
                f"{pref.get('model') or '—'}[/] "
                f"[dim](ok {pref.get('ok', 0)}/{pref.get('calls', 0)}, "
                f"{pref.get('success_rate_percent', 0)}%)[/]   "
                f"[b]Token:[/] {_human(totals.get('total_tokens'))} "
                f"[dim](prompt {_human(totals.get('prompt_tokens'))} + "
                f"completion {_human(totals.get('completion_tokens'))}, "
                f"cache {_human(totals.get('cached_tokens'))})[/]   "
                f"[b]Chiamate:[/] {totals.get('calls', 0)} "
                f"[dim](ok {totals.get('ok', 0)} / fail "
                f"{totals.get('fail', 0)} · {totals.get('success_rate_percent', 0)}%)[/]   "
                f"[b]Deployment:[/] {totals.get('deployments_ok', 0)} ok su "
                f"{totals.get('deployments', 0)}\n"
                f"[dim]sticky: {data.get('sticky') or '—'} · "
                f"holder: {(data.get('cache_holder') or {}).get('unique') or '—'} · "
                f"warm: {len(data.get('owned_deployments') or [])} · "
                f"slow: {len(data.get('slow_demoted') or [])}[/]"
            )

            t = self.query_one("#sess-detail-t", DataTable)
            t.clear()
            for r in data.get("deployments", []) or []:
                flags = r.get("session_flags") or {}
                mark = ""
                if flags.get("cache_holder"):
                    mark += "*"
                if flags.get("warm"):
                    mark += "~"
                if flags.get("slow"):
                    mark += "!"
                dep = str(r.get("deployment") or "-")
                t.add_row(
                    (mark + " " + dep)[:34] if mark else dep[:34],
                    str(r.get("model") or "-")[:22],
                    str(r.get("calls", 0)),
                    str(r.get("ok", 0)),
                    str(r.get("fail", 0)),
                    f"{r.get('success_rate_percent', 0):.1f}",
                    _human(r.get("prompt_tokens")),
                    _human(r.get("completion_tokens")),
                    _human(r.get("total_tokens")),
                    _human(r.get("cached_tokens")),
                    str(r.get("avg_duration_ms") or "-"),
                    str(r.get("fallbacks", 0)),
                    str(r.get("qc_discards", 0)),
                    str(r.get("watchdog", 0)),
                )
        finally:
            self._loading = False
