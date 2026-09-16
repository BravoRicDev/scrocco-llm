"""Editor grezzo (YAML/CSV) per la TUI.

Garantisce che OGNI configurazione del backend sia gestibile dalla TUI anche
quando non esiste un campo dedicato: carica il testo grezzo via `loader`, lo
mostra in un TextArea e lo salva via `saver`.

Usato per la policy (/admin/policy/raw) e per il CSV (/admin/csv).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, TextArea


class RawEditorScreen(ModalScreen):
    """Editor testuale grezzo con carica/salva via callback async."""

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("ctrl+s", "save", "Salva"),
        Binding("f2", "save", "Salva", show=False),
    ]

    def __init__(self, title: str,
                 loader: Callable[[], Awaitable[dict]],
                 saver: Callable[[str], Awaitable[dict]],
                 text_key: str = "raw"):
        super().__init__()
        self._title = title
        self._loader = loader
        self._saver = saver
        self._text_key = text_key
        self._loading = False

    def compose(self):
        with Vertical(id="raw-box"):
            yield Label("", id="raw-title")
            yield Label("", id="raw-status")
            yield TextArea(id="raw-area", language=None)

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        self.query_one("#raw-title", Label).update(
            f"[b cyan]{self._title}[/]  [dim]ctrl+s salva · esc chiudi[/]")
        try:
            data = await self._loader()
        except Exception as exc:                     # noqa: BLE001
            self.query_one("#raw-status", Label).update(
                f"[red]caricamento fallito: {exc}[/]")
            return
        text = ""
        if isinstance(data, dict):
            text = str(data.get(self._text_key) or data.get("raw") or "")
        try:
            self.query_one("#raw-area", TextArea).text = text
        except Exception as exc:                     # noqa: BLE001
            self.query_one("#raw-status", Label).update(f"[red]{exc}[/]")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self.run_worker(self._save(), exclusive=True)

    async def _save(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            new_text = self.query_one("#raw-area", TextArea).text
            try:
                res = await self._saver(new_text)
            except Exception as exc:                 # noqa: BLE001
                self.query_one("#raw-status", Label).update(
                    f"[red]salvataggio fallito: {exc}[/]")
                return
            ok = True
            if isinstance(res, dict) and res.get("error"):
                ok = False
            self.query_one("#raw-status", Label).update(
                "[green]salvato[/]" if ok else "[red]salvataggio rifiutato[/]")
        finally:
            self._loading = False
