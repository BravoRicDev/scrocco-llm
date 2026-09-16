from __future__ import annotations

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, TabbedContent, TabPane

from .gateway_client import GatewayClient
from .obs_live import LiveCallsPanel
from .obs_errors import ErrorsPanel
from .obs_leaderboard import LeaderboardPanel
from .obs_sessions import SessionsPanel
from .obs_stats import StatsPanel


class ObservabilityScreen(ModalScreen):
    """5 viste osservabilita: chiamate live, errori, classifica, sessioni, statistiche."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Ricarica")]

    def __init__(self, client: GatewayClient, initial: str = "tab-live"):
        super().__init__()
        self.client = client
        self.initial = initial

    def compose(self):
        with VerticalScroll():
            yield Label("[b cyan]OSSERVABILITA[/]  "
                        "[dim]1-5 cambia vista · r ricarica · esc chiudi[/]", id="form-title")
            with TabbedContent(initial=self.initial, id="obs-tabs"):
                with TabPane("Live", id="tab-live"):
                    yield LiveCallsPanel(self.client)
                with TabPane("Errori", id="tab-err"):
                    yield ErrorsPanel(self.client)
                with TabPane("Classifica", id="tab-lb"):
                    yield LeaderboardPanel(self.client)
                with TabPane("Sessioni", id="tab-sess"):
                    yield SessionsPanel(self.client)
                with TabPane("Statistiche", id="tab-stats"):
                    yield StatsPanel(self.client)

    def _active_panel(self):
        tc = self.query_one("#obs-tabs", TabbedContent)
        pane = tc.active
        mapping = {"tab-live": LiveCallsPanel, "tab-err": ErrorsPanel,
                   "tab-lb": LeaderboardPanel, "tab-sess": SessionsPanel,
                   "tab-stats": StatsPanel}
        cls = mapping.get(pane)
        return self.query_one(cls) if cls else None

    def action_refresh(self) -> None:
        panel = self._active_panel()
        if panel is not None:
            panel.run_worker(panel.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)
