"""Vista dei parametri di tuning effettivi (modal screen).

Consuma GET /admin/tuning: mostra i valori correnti delle manopole che prima
erano hardcoded (router, forwarder, admin, storage) e i valori di policy
efficaci. Ogni voce e' configurabile da gateway.yaml (policy) o, per lo
storage, da variabili d'ambiente: la TUI la rende ispezionabile.
"""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

from .gateway_client import GatewayClient, GatewayError


class TuningScreen(ModalScreen):
    """Mostra i parametri di tuning effettivi a runtime."""

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("q", "close", "Chiudi"),
        Binding("r", "refresh", "Ricarica"),
    ]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading = False

    def compose(self):
        with VerticalScroll():
            yield Label("", id="tuning-title")
            yield DataTable(id="tuning-t", zebra_stripes=True,
                            cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#tuning-t", DataTable)
        t.add_column("modulo", key="mod")
        t.add_column("parametro", key="name")
        t.add_column("valore", key="val")
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
                data = await self.client.tuning()
            except GatewayError as e:
                self.query_one("#tuning-title", Label).update(
                    f"[red]errore tuning: {e}[/]")
                return
            t = self.query_one("#tuning-t", DataTable)
            t.clear()
            for mod in ("router", "forwarder", "admin", "storage"):
                block = data.get(mod, {}) or {}
                for name, val in block.items():
                    t.add_row(mod, str(name), str(val))
            pol = data.get("policy_effective", {}) or {}
            for name, val in pol.items():
                t.add_row("policy", str(name), str(val))
            self.query_one("#tuning-title", Label).update(
                "[b cyan]TUNING EFFETTIVO[/]  "
                "[dim]configurabile da gateway.yaml (policy) · "
                "storage via env · r ricarica · esc chiudi[/]"
            )
        finally:
            self._loading = False
