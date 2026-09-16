from __future__ import annotations

import time

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Input

from . import tui_config as cfg
from .gateway_client import GatewayClient, GatewayError


class PersistedScoresScreen(ModalScreen):
    """Punteggi PERSISTITI per deployment (var/adaptive_stats.json).

    Fonte: GET /admin/deployments/stats — ok/fail cumulativi, latenza EMA,
    ultimo motivo, timestamp. Sopravvivono al restart."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Ricarica")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading: bool = False

    def compose(self):
        with Vertical(id="persist-box"):
            yield Label("[b cyan]PUNTEGGI PERSISTITI[/]  "
                        "[dim]r ricarica · esc chiudi[/]", id="persist-title")
            yield DataTable(id="persist-t", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#persist-t", DataTable)
        t.add_columns(("deployment","dep"), ("ok","ok"), ("fail","fail"), ("latenza EMA ms","ema"), ("ultimo motivo","reason"), ("ultimo uso","age"))
        self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.deployments_stats()
            except GatewayError as e:
                self.query_one("#persist-title", Label).update(
                    f"[red]errore punteggi: {e}[/]")
                return
            rows = data.get("rows", [])
            t = self.query_one("#persist-t", DataTable)
            t.clear()
            for r in rows:
                dep = (r.get("unique") or r.get("dep") or "-")
                dep = dep.rsplit("__", 1)[-1]
                ts = r.get("last_used")
                age = "-" if not ts else f"{int((time.time() - ts) // 60)}m fa"
                t.add_row(dep, str(r.get("ok", 0)), str(r.get("fail", 0)),
                          str(r.get("ema_latency_ms") or "-"),
                          str(r.get("last_reason") or "-"), age)
            self.query_one("#persist-title", Label).update(
                f"[b cyan]PUNTEGGI PERSISTITI[/]  [dim]{len(rows)} righe "
                f"· r ricarica · esc chiudi[/]")
        finally:
            self._loading = False

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)


class ProviderHealthScreen(ModalScreen):
    """Salute aggregata per provider: contatori, circuit breaker, latenze.

    Fonte: GET /admin/providers/health."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Ricarica")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading: bool = False

    def compose(self):
        with Vertical(id="provh-box"):
            yield Label("[b cyan]SALUTE PROVIDER[/]  "
                        "[dim]r ricarica · esc chiudi[/]", id="provh-title")
            yield DataTable(id="provh-t", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#provh-t", DataTable)
        t.add_columns(("provider","provider"), ("modelli","models"), ("dep","deps"), ("chiamate","calls"), ("success%","success"), ("ema ms","ema"), ("breaker","breaker"))
        self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.providers_health()
            except GatewayError as e:
                self.query_one("#provh-title", Label).update(
                    f"[red]errore provider: {e}[/]")
                return
            provs = data.get("providers") or []
            if isinstance(provs, dict):
                provs = list(provs.values())
            t = self.query_one("#provh-t", DataTable)
            t.clear()
            for p in sorted(provs, key=lambda x: x.get("total_calls", 0),
                            reverse=True):
                brk = p.get("circuit_breakers") or {}
                brk_txt = "/".join(f"{k}:{brk[k]}" for k in sorted(brk))
                t.add_row(p.get("provider") or "-",
                          ",".join(p.get("models") or [])[:40],
                          str(p.get("total_deployments", 0)),
                          str(p.get("total_calls", 0)),
                          str(p.get("success_rate", 0)),
                          str(p.get("ema_latency_ms") or "-"),
                          brk_txt or "-")
            self.query_one("#provh-title", Label).update(
                f"[b cyan]SALUTE PROVIDER[/]  [dim]{len(provs)} provider "
                f"· r ricarica · esc chiudi[/]")
        finally:
            self._loading = False

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)


class LeaderboardPanel(Vertical):
    """Vista classifica deployment. Ordinabile per colonna e filtrabile per profilo."""

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._sort: str = "calls"
        self._order: str = "desc"
        self._loading: bool = False

    def compose(self):
        yield Label(
            "[b cyan]CLASSIFICA DEPLOYMENT (7g)[/]  "
            "[dim]click intestazione = ordina · auto 15s · esc chiudi[/]\n"
            "[dim]fb%=fallback · qc%=scarto QC · wd%=watchdog · "
            "tot buono/rosso ≥20% · [b]p[/] persistiti · [b]h[/] provider[/]",
            id="obs-lb-title",
        )
        yield Input(placeholder="filtro profilo (vuoto = tutti)", id="obs-lb-profile")
        yield DataTable(id="obs-lb-t", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#obs-lb-t", DataTable)
        cols = [
            ("deployment", "dep"),
            ("profilo", "profile"),
            ("gruppo", "group"),
            ("provider", "provider"),
            ("modello", "model"),
            ("chiamate", "calls"),
            ("avg ms", "avg_dur_ms"),
            ("p95 ms", "p95_dur_ms"),
            ("fb%", "fb_rate"),
            ("qc%", "qc_rate"),
            ("wd%", "wd_rate"),
            ("tot%", "error_rate"),
            ("ultimo uso", "last_used"),
            ("health", "health"),
            ("probe ms", "probe_ms"),
        ]
        for label, key in cols:
            t.add_column(label, key=key)
        self._loading = False
        self.run_worker(self.refresh_data(), exclusive=True)
        self.set_interval(cfg.REFRESH_LEADERBOARD_SEC, self._tick)

    def _tick(self) -> None:
        if not self._loading:
            self.run_worker(self.refresh_data(), exclusive=True)

    def on_data_table_header_selected(self, event) -> None:
        key = event.column_key.value if event.column_key is not None else None
        if not key or key == "health":  # health non e' ordinabile lato server
            return
        if key == self._sort:
            self._order = "asc" if self._order == "desc" else "desc"
        else:
            self._sort = key
            self._order = "desc"
        self.run_worker(self.refresh_data(), exclusive=True)

    def on_input_changed(self, event) -> None:
        if event.input.id == "obs-lb-profile":
            self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            prof = self.query_one("#obs-lb-profile", Input).value.strip() or None
            try:
                data = await self.client.leaderboard(
                    window="7d", sort=self._sort, order=self._order, profile=prof
                )
            except GatewayError as e:
                self.query_one("#obs-lb-title", Label).update(
                    f"[red]errore classifica: {e}[/]"
                )
                return

            rows = data.get("rows", [])
            t = self.query_one("#obs-lb-t", DataTable)
            t.clear()

            def _ms(v):
                return "-" if v is None else str(int(round(v)))

            def _age(ts):
                if not ts:
                    return "mai"
                d = time.time() - ts
                if d < 60:
                    return f"{int(d)}s"
                if d < 3600:
                    return f"{int(d // 60)}m"
                if d < 86400:
                    return f"{int(d // 3600)}h"
                return f"{int(d // 86400)}g"

            def _pct(v):
                if v is None:
                    return "-"
                return f"{v * 100:.1f}"

            for r in rows:
                dep = (r.get("dep") or "-").rsplit("__", 1)[-1]
                grp = r.get("group") or "-"
                if len(grp) > 24:
                    grp = grp[:23] + "…"
                err = r.get("error_rate")
                fb = r.get("fb_rate")
                qc = r.get("qc_rate")
                wd = r.get("wd_rate")
                # celle del breakdown: evidenzia il componente che spinge l'err%
                def _src(v):
                    if v is None:
                        return "-"
                    if isinstance(v, (int, float)) and v >= 0.20:
                        return f"[red]{_pct(v)}[/]"
                    return _pct(v)
                fb_txt, qc_txt, wd_txt = _src(fb), _src(qc), _src(wd)
                err_txt = "-" if err is None else f"{err * 100:.1f}%"
                if isinstance(err, (int, float)) and err >= 0.20:
                    err_txt = f"[red]{err_txt}[/]"
                health = r.get("health") or "ok"
                if health != "ok":
                    health = f"[yellow]{health}[/]"
                t.add_row(
                    dep,
                    r.get("profile") or "-",
                    grp,
                    r.get("provider") or "-",
                    r.get("model") or "-",
                    str(r.get("calls", 0)),
                    _ms(r.get("avg_dur_ms")),
                    _ms(r.get("p95_dur_ms")),
                    fb_txt,
                    qc_txt,
                    wd_txt,
                    err_txt,
                    _age(r.get("last_used")),
                    health,
                    _ms(r.get("probe_ms")),
                )

            self.query_one("#obs-lb-title", Label).update(
                f"[b cyan]CLASSIFICA DEPLOYMENT (7g)[/]  "
                f"[dim]ordina: {self._sort} {self._order} · click intestazione "
                f"· auto 15s · esc[/]\n"
                f"[dim]fb%=fallback · qc%=scarto QC · wd%=watchdog · "
                f"tot% = somma · rosso ≥ 20%[/]"
            )
        finally:
            self._loading = False
