from __future__ import annotations

from datetime import datetime

from textual.containers import Vertical
from textual.widgets import DataTable, Input, Label

from .gateway_client import GatewayClient, GatewayError


class ErrorsPanel(Vertical):
    """Vista errori tracciati."""

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._loading = False

    def compose(self):
        yield Label(
            "[b cyan]ERRORI TRACCIATI[/]  [dim]non bloccano il gateway · auto 5s · / filtra · esc chiudi[/]",
            id="obs-err-title",
        )
        yield Label("", id="obs-err-stats")
        yield Input(placeholder="filtro (status o testo)", id="obs-err-filter")
        yield DataTable(id="obs-err-t", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#obs-err-t", DataTable)
        t.add_column("ora", key="ts")
        t.add_column("status", key="status")
        t.add_column("tipo", key="type")
        t.add_column("messaggio", key="message")
        self._loading = False
        self.run_worker(self.refresh_data(), exclusive=True)
        self.set_interval(5.0, self._tick)

    def _tick(self) -> None:
        if not self._loading:
            self.run_worker(self.refresh_data(), exclusive=True)

    def on_input_changed(self, event) -> None:
        if event.input.id == "obs-err-filter":
            self.run_worker(self.refresh_data(), exclusive=True)

    def on_input_submitted(self, event) -> None:
        if event.input.id == "obs-err-filter":
            self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            filt = self.query_one("#obs-err-filter", Input).value.strip() or None
            try:
                data = await self.client.errors(filter=filt, tail=300)
            except GatewayError as e:
                self.query_one("#obs-err-title", Label).update(
                    f"[red]errore gateway: {e}[/]"
                )
                return
            events = data.get("events", [])
            t = self.query_one("#obs-err-t", DataTable)
            t.clear()
            # conteggi riepilogativi per status, così l'informazione resta
            # visibile anche quando la finestra è grande.
            c500 = c429 = c4xx = other = 0
            for ev in events:
                ora = (
                    datetime.fromtimestamp(int(ev["ts"])).strftime("%H:%M:%S")
                    if ev.get("ts")
                    else "-"
                )
                st = ev.get("status")
                sev = isinstance(st, int) and (st >= 500 or st < 0)
                st_txt = f"[red]{st}[/]" if sev else str(st)
                tipo = ev.get("error_type") or "-"
                msg = (ev.get("error_message") or "-").replace("\n", " ")
                if len(msg) > 120:
                    msg = msg[:119] + "…"
                t.add_row(ora, st_txt, tipo, msg)
                # riepilogo
                if isinstance(st, int) and st < 0:
                    c4xx += 1
                elif st == 429:
                    c429 += 1
                elif isinstance(st, int) and st >= 500:
                    c500 += 1
                else:
                    other += 1
            self.query_one("#obs-err-stats", Label).update(
                f"[dim]riepilogo (coda): 5xx={c500} · 429={c429} · "
                f"4xx/neg={c4xx} · altro={other}[/]"
            )
            self.query_one("#obs-err-title", Label).update(
                f"[b cyan]ERRORI TRACCIATI[/] [dim]({len(events)}) · auto 5s · / filtra · esc[/]"
            )
        finally:
            self._loading = False
