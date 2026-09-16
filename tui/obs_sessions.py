"""Vista sessioni attive + classifica sessioni per la TUI.

Mostra due tabelle:
  1) stato runtime delle sessioni attive (sticky, dep-sticky, cache holder,
     dep-guard, slow demote) da GET /admin/sessions;
  2) classifica delle sessioni da GET /admin/stats/sessions (chiamate, ok,
     fail, success rate, token, modello preferito).

Invio su una riga della classifica apre il DETTAGLIO della sessione con la
classifica dei deployment che vi hanno partecipato con successo.
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import DataTable, Label

from . import tui_config as cfg
from .gateway_client import GatewayClient, GatewayError
from .session_detail import SessionDetailScreen


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


class SessionsPanel(Vertical):
    """Sessioni attive + classifica; invio -> dettaglio deployment."""

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading = False
        self._lb_sessions: list[dict] = []

    def compose(self):
        yield Label(
            "[b cyan]SESSIONI[/]  "
            "[dim]r ricarica · auto 10s · invio=dettaglio · esc chiudi[/]",
            id="sess-title",
        )
        yield Label("[b]Classifica sessioni (7g)[/]", id="sess-lb-sub")
        yield DataTable(id="sess-lb", zebra_stripes=True, cursor_type="row")
        yield Label("[b]Stato runtime sessioni attive[/]", id="sess-state-sub")
        yield DataTable(id="sess-t", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        lb = self.query_one("#sess-lb", DataTable)
        for label, key in [
            ("sessione", "sid"), ("chiamate", "calls"), ("ok", "ok"),
            ("fail", "fail"), ("success%", "sr"), ("tot tok", "tt"),
            ("dep", "deps"), ("modello preferito", "pref"),
        ]:
            lb.add_column(label, key=key)
        t = self.query_one("#sess-t", DataTable)
        for label, key in [("tipo", "type"), ("sessione", "session_id"),
                           ("target", "target"), ("eta", "age"),
                           ("dettagli", "details")]:
            t.add_column(label, key=key)
        self.run_worker(self.refresh_data(), exclusive=True)
        self.set_interval(cfg.REFRESH_SESSIONS_SEC, self._tick)

    def _tick(self) -> None:
        if not self._loading:
            self.run_worker(self.refresh_data(), exclusive=True)

    def on_data_table_row_selected(self, event) -> None:
        if event.data_table.id != "sess-lb":
            return
        try:
            idx = event.cursor_row
        except Exception:                        # noqa: BLE001
            return
        if 0 <= idx < len(self._lb_sessions):
            sid = self._lb_sessions[idx].get("session_id")
            if sid:
                self.app.push_screen(SessionDetailScreen(self.client, sid))

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.sessions()
                lb = await self.client.stats_sessions("7d", 100)
            except GatewayError as e:
                self.query_one("#sess-title", Label).update(
                    f"[red]errore sessioni: {e}[/]")
                return

            # ------------------------------------------------ classifica
            lb_t = self.query_one("#sess-lb", DataTable)
            lb_t.clear()
            self._lb_sessions = lb.get("sessions", []) or []
            for s in self._lb_sessions:
                lb_t.add_row(
                    str(s.get("session_id") or "-")[:26],
                    str(s.get("calls", 0)),
                    str(s.get("ok", 0)),
                    str(s.get("fail", 0)),
                    f"{s.get('success_rate_percent', 0):.1f}",
                    _human(s.get("total_tokens")),
                    str(s.get("deployments", 0)),
                    str(s.get("preferred_model") or "-")[:26],
                )

            # ------------------------------------------------ stato runtime
            t = self.query_one("#sess-t", DataTable)
            t.clear()
            sticky = data.get("sticky_sessions", []) or []
            dep_sticky = data.get("dep_sticky_sessions", []) or []
            session_deps = data.get("session_deps", []) or []
            cache_holders = data.get("cache_holders", []) or []
            slow = data.get("slow_demoted", []) or []
            for s in sticky:
                t.add_row("sticky", str(s.get("session_id") or "-")[:20],
                          str(s.get("target") or "-")[:32],
                          _fmt_age(s.get("age_sec")),
                          f"ttl={s.get('ttl_sec', '?')}s")
            for s in dep_sticky:
                t.add_row("dep-sticky", str(s.get("session_id") or "-")[:20],
                          str(s.get("unique") or "-")[:32],
                          _fmt_age(s.get("age_sec")),
                          f"ttl={s.get('ttl_sec', '?')}s")
            for s in cache_holders:
                t.add_row("cache-holder", str(s.get("session_id") or "-")[:20],
                          str(s.get("unique") or "-")[:32],
                          _fmt_age(s.get("age_sec")),
                          f"ttl={s.get('ttl_sec', '?')}s")
            for s in session_deps:
                t.add_row("dep-guard", str(s.get("session_id") or "-")[:20],
                          f"{s.get('owned_count', 0)} dep", "-",
                          ",".join(str(u)[:10]
                                   for u in (s.get("uniques") or [])[:3]))
            for s in slow:
                hard = "H" if s.get("hard") else "S"
                t.add_row(f"slow-{hard}", str(s.get("session_id") or "-")[:20],
                          str(s.get("unique") or "-")[:32],
                          _fmt_age(s.get("age_sec")),
                          "hard" if s.get("hard") else "soft")

            totals = data.get("totals", {}) or {}
            self.query_one("#sess-title", Label).update(
                f"[b cyan]SESSIONI[/]  "
                f"[dim]in classifica: {lb.get('sessions_count', 0)} · "
                f"attive: sticky={totals.get('sticky', len(sticky))} "
                f"dep-sticky={totals.get('dep_sticky', len(dep_sticky))} "
                f"cache={totals.get('cache_holders', len(cache_holders))} "
                f"dep-guard={totals.get('session_deps', len(session_deps))} "
                f"slow={totals.get('slow_demoted', len(slow))} · "
                f"invio=dettaglio[/]"
            )
        finally:
            self._loading = False
