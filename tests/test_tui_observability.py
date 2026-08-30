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
