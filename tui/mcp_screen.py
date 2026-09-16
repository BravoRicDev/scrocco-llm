"""Browser ed esecutore dei tool MCP di configurazione (modal screen).

Consuma il protocollo MCP esposto dal gateway:
  - GET  /admin/mcp/config/tools   -> elenco tool + inputSchema
  - POST /admin/mcp/config/execute -> esegue {tool, arguments}

Permette di ispezionare ogni tool di configurazione e di eseguirlo con
argomenti JSON, cosi' OGNI configurazione del backend e' gestibile anche
dalla TUI (parita' con API e MCP).
"""
from __future__ import annotations

import json

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Input, Label, OptionList, Static)
from textual.widgets.option_list import Option

from . import tui_config as cfg
from .gateway_client import GatewayClient, GatewayError


class McpConfigScreen(ModalScreen):
    """Sfoglia ed esegue i tool MCP di configurazione."""

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("q", "close", "Chiudi"),
        Binding("r", "refresh", "Ricarica tool"),
        Binding("ctrl+r", "execute", "Esegui"),
        Binding("ctrl+e", "execute", "Esegui"),
    ]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._tools: list[dict] = []
        self._by_name: dict[str, dict] = {}
        self._loading = False

    def compose(self):
        yield Label("", id="mcp-title")
        with Horizontal(id="mcp-body"):
            with Vertical(id="mcp-left"):
                yield Label("[b]Tool di configurazione[/]", id="mcp-left-h")
                yield OptionList(id="mcp-tools")
            with Vertical(id="mcp-right"):
                yield Static("", id="mcp-detail")
                yield Label("[b]Argomenti (JSON)[/]", id="mcp-args-h")
                yield Input(id="mcp-args", placeholder='{"window":"7d"}')
                yield Label("[b]Risultato[/]", id="mcp-res-h")
                yield Static("", id="mcp-result")

    def on_mount(self) -> None:
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_execute(self) -> None:
        self.run_worker(self._execute(), exclusive=True)

    def on_option_list_option_selected(self, event) -> None:
        name = str(event.option.id)
        self._show_tool(name)

    def _selected_tool(self) -> str | None:
        ol = self.query_one("#mcp-tools", OptionList)
        if ol.highlighted is None or not self._tools:
            return None
        idx = ol.highlighted
        if 0 <= idx < len(self._tools):
            return self._tools[idx]["name"]
        return None

    def _show_tool(self, name: str) -> None:
        spec = self._by_name.get(name)
        if not spec:
            return
        schema = spec.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        lines = [
            f"[b cyan]{name}[/]",
            str(spec.get("description") or ""),
            "",
            "[b]Parametri:[/]",
        ]
        if props:
            for pname, pspec in props.items():
                star = "*" if pname in req else ""
                lines.append(
                    f"  [green]{pname}{star}[/] "
                    f"[dim]({pspec.get('type', 'any')})[/]")
        else:
            lines.append("  [dim](nessuno)[/]")
        self.query_one("#mcp-detail", Static).update("\n".join(lines))

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.mcp_tools()
            except GatewayError as e:
                self.query_one("#mcp-title", Label).update(
                    f"[red]errore MCP: {e}[/]")
                return
            self._tools = data.get("tools", []) or []
            self._by_name = {t["name"]: t for t in self._tools}
            ol = self.query_one("#mcp-tools", OptionList)
            ol.clear_options()
            for t in self._tools:
                ol.add_option(Option(t["name"], id=t["name"]))
            if self._tools:
                ol.highlighted = 0
                self._show_tool(self._tools[0]["name"])
            self.query_one("#mcp-title", Label).update(
                f"[b cyan]MCP CONFIG[/]  [dim]tool: {len(self._tools)} · "
                f"r ricarica · ctrl+r esegui · esc chiudi[/]")
        finally:
            self._loading = False

    async def _execute(self) -> None:
        name = self._selected_tool()
        if not name:
            self.query_one("#mcp-result", Static).update(
                "[yellow]nessun tool selezionato[/]")
            return
        raw = self.query_one("#mcp-args", Input).value.strip()
        args: dict = {}
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                self.query_one("#mcp-result", Static).update(
                    f"[red]JSON non valido: {e}[/]")
                return
            if not isinstance(args, dict):
                self.query_one("#mcp-result", Static).update(
                    "[red]gli argomenti devono essere un oggetto JSON[/]")
                return
        self.query_one("#mcp-result", Static).update("[dim]esecuzione…[/]")
        try:
            res = await self.client.mcp_execute(name, args)
        except GatewayError as e:
            self.query_one("#mcp-result", Static).update(
                f"[red]errore: {e}[/]")
            return
        try:
            text = res["content"][0]["text"]
        except Exception:                        # noqa: BLE001
            text = json.dumps(res, ensure_ascii=False)
        try:
            pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except Exception:                        # noqa: BLE001
            pretty = text
        head = pretty[:cfg.MCP_RESULT_MAX_CHARS]
        flag = "[red]ERRORE[/] " if res.get("isError") else "[green]OK[/] "
        self.query_one("#mcp-result", Static).update(flag + head)
