"""Vista statistiche complete per la TUI.

Consuma GET /admin/stats/summary (token 1h/24h, cache, success rate,
classifica modelli) e GET /admin/stats/models (classifica estesa).
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import DataTable, Label, Static

from . import tui_config as cfg
from .gateway_client import GatewayClient, GatewayError


def _human_tokens(n) -> str:
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


class StatsPanel(Vertical):
    """Vista statistiche complete: token, cache, success rate, classifica modelli."""

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading = False

    def compose(self):
        yield Label(
            "[b cyan]STATISTICHE COMPLETE[/]  "
            "[dim]r ricarica · auto 30s · esc chiudi[/]",
            id="stats-title",
        )
        yield Static("", id="stats-summary")
        yield Label("[b]Classifica modelli (7g)[/]", id="stats-sub")
        yield DataTable(id="stats-t", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#stats-t", DataTable)
        for label, key in [
            ("modello", "model"), ("chiamate", "calls"), ("ok", "ok"),
            ("fail", "fail"), ("success%", "sr"), ("prompt tok", "pt"),
            ("completion tok", "ct"), ("tot tok", "tt"), ("costo", "cost"),
            ("avg ms", "avg"), ("fb%", "fb"), ("qc%", "qc"),
        ]:
            t.add_column(label, key=key)
        self.run_worker(self.refresh_data(), exclusive=True)
        self.set_interval(cfg.REFRESH_STATS_SEC, self._tick)

    def _tick(self) -> None:
        if not self._loading:
            self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                summary = await self.client.stats_summary()
                models = await self.client.stats_models("7d")
            except GatewayError as e:
                self.query_one("#stats-title", Label).update(
                    f"[red]errore statistiche: {e}[/]")
                return

            t24 = summary.get("tokens_24h", {}) or {}
            t1 = summary.get("tokens_1h", {}) or {}
            cache = summary.get("cache", {}) or {}
            sr = summary.get("success_rate", {}) or {}
            runtime = summary.get("runtime", {}) or {}
            pref = summary.get("preferred_model") or {}
            pref_cfg = summary.get("preferred_models_config") or {}

            summary_txt = (
                f"[b]Token 24h[/] {_human_tokens(t24.get('total_tokens'))} "
                f"[dim](prompt {_human_tokens(t24.get('prompt_tokens'))} + "
                f"completion {_human_tokens(t24.get('completion_tokens'))})[/]  "
                f"[b]Token 1h[/] {_human_tokens(t1.get('total_tokens'))}   "
                f"[b]Cache hit[/] {cache.get('hit_rate', 0)}% "
                f"[dim]({cache.get('hits', 0)}/{cache.get('total_requests', 0)})[/]  "
                f"[b]Coalesce[/] {cache.get('coalesce_hits', 0)}   "
                f"[b]Success[/] {sr.get('rate_percent', 0)}% "
                f"[dim]({sr.get('total_ok', 0)}/{sr.get('total_calls', 0)})[/]\n"
                f"[b]Modello preferito (24h)[/] [green]{pref.get('model') or '—'}[/] "
                f"[dim](ok {pref.get('ok', 0)}/{pref.get('calls', 0)}, "
                f"{pref.get('success_rate_percent', 0)}%)[/]   "
                f"[b]Config -go preferiti[/] "
                f"[cyan]{', '.join(pref_cfg.get('go_preferred_models') or []) or '—'}[/]\n"
                f"[dim]chiamate 24h: {t24.get('calls', 0)} · "
                f"costo 24h: ${t24.get('cost_reported_usd', 0)} "
                f"(stimato ${t24.get('cost_estimated_usd', 0)}) · "
                f"deployment tracciati: {runtime.get('tracked_deployments', 0)} · "
                f"cooldown: {runtime.get('cooldowns_active', 0)} · "
                f"sticky: {runtime.get('sticky_sessions', 0)}[/]"
            )
            self.query_one("#stats-summary", Static).update(summary_txt)

            t = self.query_one("#stats-t", DataTable)
            t.clear()
            ranking = models.get("ranking", []) or []
            if not ranking:
                # fallback: usa il model_ranking dello stats/summary
                for row in (summary.get("model_ranking", []) or [])[:cfg.MODEL_RANKING_MAX]:
                    t.add_row(
                        str(row.get("model") or "-")[:26],
                        str(row.get("calls", 0)), str(row.get("ok", 0)),
                        str(row.get("fail", 0)),
                        f"{row.get('success_rate', 0):.1f}",
                        "-", "-", "-", "-", "-", "-", "-",
                    )
            else:
                for row in ranking[:cfg.MODEL_RANKING_MAX]:
                    cost = (row.get("cost_reported_usd", 0)
                            or row.get("cost_estimated_usd", 0))
                    avg = row.get("avg_latency_ms")
                    t.add_row(
                        str(row.get("model") or "-")[:26],
                        str(row.get("calls", 0)),
                        str(row.get("ok", 0)),
                        str(row.get("fail", 0)),
                        f"{row.get('success_rate_percent', 0):.1f}",
                        str(row.get("prompt_tokens", 0)),
                        str(row.get("completion_tokens", 0)),
                        str(row.get("total_tokens", 0)),
                        f"${cost:.4f}" if cost else "-",
                        str(int(avg)) if avg else "-",
                        f"{row.get('fb_rate_percent', 0):.1f}",
                        f"{row.get('qc_rate_percent', 0):.1f}",
                    )

            self.query_one("#stats-title", Label).update(
                f"[b cyan]STATISTICHE COMPLETE[/]  "
                f"[dim]modelli: {models.get('models_count', len(ranking))} · "
                f"r ricarica · auto 30s[/]"
            )
        finally:
            self._loading = False
