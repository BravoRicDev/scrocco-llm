from __future__ import annotations

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, TabbedContent, TabPane

from .gateway_client import GatewayClient
from .obs_live import LiveCallsPanel
from .obs_errors import ErrorsPanel
from .obs_leaderboard import LeaderboardPanel


class ObservabilityScreen(ModalScreen[None]):
    """3 viste osservabilità: chiamate live, errori tracciati, classifica deployment."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Ricarica")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client

    def compose(self):
        with VerticalScroll(id="modal-box"):
            yield Label("[b cyan]OSSERVABILITÀ[/] · [dim]1/2/3 cambia vista · "
                        "r ricarica · esc chiudi[/]", id="form-title")
            with TabbedContent(initial="tab-live"):
                with TabPane("Live", id="tab-live"):
                    yield LiveCallsPanel(self.client)
                with TabPane("Errori", id="tab-err"):
                    yield ErrorsPanel(self.client)
                with TabPane("Classifica", id="tab-lb"):
                    yield LeaderboardPanel(self.client)

    def _active_panel(self):
        tc = self.query_one(TabbedContent)
        pane = tc.active
        mapping = {"tab-live": LiveCallsPanel, "tab-err": ErrorsPanel,
                   "tab-lb": LeaderboardPanel}
        cls = mapping.get(pane)
        return self.query_one(cls) if cls else None

    def action_refresh(self) -> None:
        panel = self._active_panel()
        if panel is not None:
            self.run_worker(panel.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)

    # se TabbedContent non supporta i binding numerici, ignorali: r + click
    # sui tab bastano.
