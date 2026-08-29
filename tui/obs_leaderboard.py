from __future__ import annotations

import time

from textual.containers import Vertical
from textual.widgets import DataTable, Label, Input

from .gateway_client import GatewayClient, GatewayError


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
            "tot buono/rosso ≥20%[/]",
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
        self.set_interval(15.0, self._tick)

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
