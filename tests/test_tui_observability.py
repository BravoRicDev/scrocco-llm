"""Smoke test headless della schermata TUI di osservabilità.

Monta ObservabilityScreen con un client fittizio e verifica che i tre
pannelli (chiamate live / errori / classifica) si popolino senza
sollevare eccezioni.

Il test è marcato skip se textual non è installato.
"""
import asyncio
import pytest

try:
    import textual  # noqa: F401
    from textual.app import App
    from textual.widgets import DataTable

    from tui.observability import ObservabilityScreen
    _HAVE_TEXTUAL = True
    _AppBase = App
except Exception:  # pragma: no cover - textual assente
    _HAVE_TEXTUAL = False
    _AppBase = object

pytestmark = pytest.mark.skipif(
    not _HAVE_TEXTUAL, reason="textual non installato"
)


class _FakeClient:
    """Client fittizio: i 3 metodi async ritornano payload minimi."""

    async def calls(self, **k):
        return {"events": [
            {"ts": 1787000000.0, "tag": "summary", "profile": "p", "grp": "g",
             "dep": "g__m__0", "model": "prov/m", "dur_ms": 12, "tries": 1,
             "fb": 0, "qc": False, "via": "api.llm7.io", "ttfb_ms": 300, "status": None, "raw": {}}]}

    async def errors(self, **k):
        return {"events": [
            {"ts": 1787000000.0, "status": 500, "error_type": "X",
             "error_message": "boom"}]}

    async def leaderboard(self, **k):
        return {"window_days": 7, "count": 1, "rows": [
            {"dep": "g__m__0", "profile": "p", "group": "g", "provider": "prov",
             "model": "prov/m", "calls": 3, "avg_dur_ms": 10, "p95_dur_ms": 20,
             "error_rate": 0.0, "fb_rate": 0.0, "qc_rate": 0.05, "wd_rate": 0.0, "last_used": None, "health": None,
             "probe_ms": None}]}

    async def sessions(self, **k):
        return {"sticky_sessions": [
            {"session_id": "s1", "type": "group", "target": "g",
             "age_sec": 5, "ttl_sec": 3600}],
            "dep_sticky_sessions": [], "session_deps": [], "cache_holders": [],
            "slow_demoted": [],
            "totals": {"sticky": 1, "dep_sticky": 0, "session_deps": 0,
                       "cache_holders": 0, "slow_demoted": 0}}

    async def stats_sessions(self, window="7d", limit=50, **k):
        return {"window": window, "window_seconds": 604800,
                "sessions_count": 1, "sessions": [
                    {"session_id": "s1", "calls": 3, "ok": 3, "fail": 0,
                     "success_rate_percent": 100.0, "total_tokens": 1234,
                     "deployments": 1, "preferred_model": "prov/m",
                     "last_ts": 1787000000.0}]}

    async def stats_summary(self, **k):
        return {"runtime": {"tracked_deployments": 1, "cooldowns_active": 0,
                            "sticky_sessions": 1, "cache_holders": 0},
                "tokens_1h": {"calls": 1, "total_tokens": 100},
                "tokens_24h": {"calls": 3, "total_tokens": 1234,
                               "prompt_tokens": 1000, "completion_tokens": 234,
                               "cost_reported_usd": 0.0,
                               "cost_estimated_usd": 0.0},
                "cache": {"hit_rate": 0, "hits": 0, "total_requests": 0,
                          "coalesce_hits": 0},
                "success_rate": {"total_ok": 3, "total_fail": 0,
                                 "total_calls": 3, "rate_percent": 100.0},
                "preferred_model": {"model": "prov/m", "ok": 3, "calls": 3,
                                    "success_rate_percent": 100.0},
                "preferred_models_config": {"go_preferred_models": ["prov/m"]},
                "model_ranking": []}

    async def stats_models(self, window="7d", **k):
        return {"window": window, "window_days": 7.0, "models_count": 1,
                "ranking": [
                    {"model": "prov/m", "calls": 3, "ok": 3, "fail": 0,
                     "success_rate_percent": 100.0, "avg_latency_ms": 10,
                     "prompt_tokens": 1000, "completion_tokens": 234,
                     "total_tokens": 1234, "fb_rate_percent": 0.0,
                     "qc_rate_percent": 0.0}]}

    async def deployments_stats(self, **k):
        return {"count": 0, "rows": []}

    async def providers_health(self, **k):
        return {"providers": [], "circuit_breaker_config": {}}

    async def guide(self, **k):
        return {"text": "# Guida\nContenuto di prova."}

    async def history(self, limit=50, **k):
        return {"total": 0, "entries": []}

    async def purge_profile(self, profile, **k):
        return {"ok": True, "purged": profile,
                "columns": [profile, None]}

    async def unretire(self, unique, **k):
        return {"ok": True, "unique": unique, "state": "healthy"}

    async def capabilities_seed(self, dry_run=False, **k):
        return {"dry_run": dry_run, "proposals": []}

    async def playground(self, model, messages, **k):
        return {"ok": True, "model": model, "group": "g",
                "attempts": 1, "trace": []}

    async def probe_bulk(self, filter="all", force=False, **k):
        return {"filter": filter, "count": 0, "results": []}

    async def probe_one(self, unique, force=False, **k):
        return {"ok": True, "unique": unique}

    async def backups(self, **k):
        return {"dir": "/tmp", "csv": [], "yaml": []}

    async def restore_backup(self, filename, **k):
        return {"ok": True, "restored": filename}

    async def insights(self, days=7, group_by="model", **k):
        return {"total": 0, "by_model": {}}

    async def insights_summary(self, **k):
        return {"window_hours": 24, "calls": 0, "total_tokens": 0}

    async def tuning(self, **k):
        return {"router": {}, "forwarder": {}, "admin": {},
                "storage": {}, "policy_effective": {}}

    async def mcp_tools(self, **k):
        return {"tools": [], "count": 0}

    async def mcp_execute(self, tool, arguments=None, **k):
        return {"content": [{"type": "text", "text": "{}"}],
                "isError": False}

    async def mcp_call(self, method, params=None, rpc_id=1, **k):
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}

    async def csv_raw(self, **k):
        return {"text": ""}

    async def put_csv_raw(self, raw, **k):
        return {"ok": True}

    async def policy_raw(self, **k):
        return {"text": ""}

    async def put_policy_raw(self, raw, **k):
        return {"ok": True}

    async def clear_cooldowns(self, unique=None, **k):
        return {"cleared": []}

    async def release_sessions(self, session_id=None, **k):
        return {"released": []}

    async def reload(self, **k):
        return {"ok": True}

    async def models(self, **k):
        return []

    async def state(self, **k):
        return {"service": "test", "sticky_sessions": [],
                "cooldowns_active": []}

    async def profiles(self, **k):
        return [{"name": "p", "deployments": 0}]

    async def deployments(self, profile=None, **k):
        return []

    async def create_deployment(self, payload, **k):
        return {"ok": True}

    async def update_deployment(self, dep_id, patch, **k):
        return {"ok": True}

    async def delete_deployment(self, dep_id, **k):
        return {"ok": True}

    async def bulk(self, operations, **k):
        return {"ok": True}

    async def expiring(self, days=7, **k):
        return []

    async def policy_get(self, **k):
        return {"file": "x", "effective": {}}

    async def policy_patch(self, patch, **k):
        return {"ok": True}

    async def session_detail(self, session_id, window="7d", **k):
        return {"session_id": session_id, "deployments": [],
                "preferred_model": None}

    async def stats_sessions(self, window="7d", limit=50, **k):
        return {"window": window, "sessions_count": 0, "sessions": []}

    async def stats_summary(self, **k):
        return {"tokens_1h": {}, "tokens_24h": {}, "cache": {},
                "success_rate": {}, "preferred_model": None,
                "preferred_models_config": {}, "model_ranking": []}

    async def stats_tokens(self, window="24h", **k):
        return {"window": window, "by_model": {}}

    async def stats_cache(self, **k):
        return {"cache_hit_rate_percent": 0}

    async def stats_models(self, window="7d", **k):
        return {"window": window, "window_days": 7.0, "models_count": 1,
                "ranking": [
                    {"model": "prov/m", "calls": 3, "ok": 3, "fail": 0,
                     "success_rate_percent": 100.0, "avg_latency_ms": 10,
                     "prompt_tokens": 1000, "completion_tokens": 234,
                     "total_tokens": 1234, "fb_rate_percent": 0.0,
                     "qc_rate_percent": 0.0}]}

    async def stats_deployments(self, profile=None, sort="success_rate",
                                  order="desc", **k):
        return {"rows": []}

    async def stats_providers(self, **k):
        return {"providers": []}

    async def tuning(self, **k):
        return {"router": {}, "forwarder": {}, "admin": {},
                "storage": {}, "policy_effective": {}}

    async def guide(self, **k):
        return {"text": "# Guida\nContenuto di prova."}


class _TestApp(_AppBase):
    """App minima di test: monta solo ObservabilityScreen via push_screen."""

    def __init__(self, client):
        super().__init__()
        self._client = client

    def on_mount(self) -> None:
        self.push_screen(ObservabilityScreen(self._client))


def test_observability_screen_populates():
    async def _main():
        app = _TestApp(_FakeClient())
        async with app.run_test() as pilot:
            # lascia montare la schermata e girare i worker/refresh
            for _ in range(5):
                await pilot.pause()
                await asyncio.sleep(0.02)

            # la ObservabilityScreen e' uno ModalScreen nello stack: query sulla
            # sua istanza (pilot.app.query_one delega alla base screen _default).
            screen = next(
                s for s in pilot.app.screen_stack
                if isinstance(s, ObservabilityScreen)
            )
            live = screen.query_one("#obs-live-t", DataTable)
            err = screen.query_one("#obs-err-t", DataTable)
            lb = screen.query_one("#obs-lb-t", DataTable)

            assert live.row_count >= 1, "tabella chiamate live non popolata"
            assert len(live.columns) >= 11, \
                "colonne live (via/ttfb) non presenti"
            live_row_text = " ".join(str(c) for c in live.get_row_at(0))
            assert "api.llm7.io" in live_row_text, "via non mostrata in live"
            assert err.row_count >= 1, "tabella errori non popolata"
            assert lb.row_count >= 1, "tabella classifica non popolata"
            assert len(lb.columns) >= 15, \
                "colonne classifica (fb/qc/wd breakdown) assenti"

    asyncio.run(_main())
