"""Hub operativo: rende raggiungibili dalla TUI TUTTE le operazioni admin.

Copre probe (singolo/bulk), unretire, purge profilo, capabilities audit/seed,
pressure inspect/clear, backup list/restore, insights, history, guide e
playground. Ogni voce chiama la stessa admin API usata da HTTP e MCP, cosi'
la logica resta unica.
"""
from __future__ import annotations

import json

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from . import tui_config as cfg
from .gateway_client import (GatewayClient, GatewayError,
                             backup_filenames)
from .modals import ConfirmModal, TextInputModal

OPS = [
    ("probe_all", "Valida TUTTE le chiavi (probe bulk)"),
    ("probe_one", "Valida UN deployment (per unique)"),
    ("unretire", "Riattiva un deployment ritirato (per unique)"),
    ("purge_profile", "Elimina la colonna del profilo corrente (0 dep.)"),
    ("caps_audit", "Audit capacita (server-side)"),
    ("caps_seed", "Seed capacita da mappa (anteprima dry-run)"),
    ("pressure_inspect", "Ispeziona pressione / cooldown"),
    ("pressure_clear", "Azzera cooldown (unique / model / tutti)"),
    ("backups", "Backup: elenca e ripristina"),
    ("insights", "Insights uso/costi (giorni, raggruppamento)"),
    ("history", "Journal operazioni"),
    ("guide", "Guida agenti (docs/AGENT.md)"),
    ("playground", "Playground: simula una chat (trace di routing)"),
]


class OperationsScreen(ModalScreen):
    """Menu di operazioni admin non coperte da schermate dedicate."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Pulisci")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._busy = False

    def compose(self):
        with Vertical(id="ops-box"):
            yield Label("[b cyan]STRUMENTI[/] · [dim]invio esegue · "
                        "r pulisce · esc chiudi[/]", id="ops-title")
            yield OptionList(*[Option(text, id=oid) for oid, text in OPS],
                             id="ops-menu")
            yield Label("", id="ops-status")
            yield Static("", id="ops-result")

    def on_mount(self) -> None:
        self.query_one("#ops-menu", OptionList).focus()

    def action_refresh(self) -> None:
        self.query_one("#ops-result", Static).update("")
        self.query_one("#ops-status", Label).update("")

    def _status(self, text: str) -> None:
        self.query_one("#ops-status", Label).update(text)

    def _result(self, text: str) -> None:
        self.query_one("#ops-result", Static).update(text)

    def on_option_list_option_selected(self, event) -> None:
        if self._busy:
            return
        self.run_worker(self._run(str(event.option.id)), exclusive=True)

    async def _run(self, oid: str) -> None:
        self._busy = True
        try:
            handler = getattr(self, f"_op_{oid}", None)
            if handler is None:
                self._status(f"[yellow]operazione sconosciuta: {oid}[/]")
                return
            await handler()
        except GatewayError as exc:
            self._status(f"[red]{exc.message}[/]")
        except Exception as exc:                 # noqa: BLE001
            self._status(f"[red]errore: {exc}[/]")
        finally:
            self._busy = False

    async def _ask(self, title: str, placeholder: str = "",
                   default: str = "") -> str | None:
        return await self.app.push_screen_wait(
            TextInputModal(title, placeholder=placeholder, default=default))

    async def _confirm(self, question: str) -> bool:
        return bool(await self.app.push_screen_wait(ConfirmModal(question)))

    def _dump(self, data) -> str:
        try:
            return json.dumps(data, ensure_ascii=False, indent=1, default=str)
        except Exception:                        # noqa: BLE001
            return str(data)

    def action_close(self) -> None:
        self.dismiss(None)

    # ------------------------------------------------------------ handlers
    async def _op_probe_all(self) -> None:
        if not await self._confirm(
                "Validare TUTTE le chiavi? Una chiamata reale per "
                "deployment (le free-tier contano le chiamate)."):
            return
        self._status("[dim]probe in corso…[/]")
        res = await self.client.probe_bulk("all", force=False)
        rows = res.get("results") or []
        okn = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
        self._status(f"[green]probe: {okn}/{len(rows)} ok "
                     f"(filter={res.get('filter')})[/]")
        self._result("\n".join(
            f"{'OK ' if r.get('ok') else 'KO '}"
            f"{(r.get('unique') or r.get('id') or '?')[-46:]}"
            f"  {r.get('reason') or r.get('error') or ''}"
            for r in rows[:cfg.OPS_ROWS_MAX] if isinstance(r, dict)))

    async def _op_probe_one(self) -> None:
        unique = await self._ask("Unique del deployment da validare")
        if not unique:
            return
        self._status("[dim]probe…[/]")
        res = await self.client.probe_one(unique, force=False)
        self._status("[green]probe eseguito[/]" if res.get("ok")
                     else f"[yellow]{res.get('reason') or 'ko'}[/]")
        self._result(self._dump(res))

    async def _op_unretire(self) -> None:
        unique = await self._ask("Unique del deployment da riattivare")
        if not unique:
            return
        res = await self.client.unretire(unique)
        self._status(f"[green]riattivato: {res.get('unique')} "
                     f"-> {res.get('state')}[/]")
        self._result(self._dump(res))

    async def _op_purge_profile(self) -> None:
        pname = self.app.current_profile()
        if not pname:
            self._status("[yellow]nessun profilo selezionato[/]")
            return
        typed = await self._ask(
            f"Digita il nome del profilo '{pname}' per ELIMINARE la colonna "
            "(deve avere 0 deployment)", default="")
        if typed != pname:
            self._status("[yellow]annullato (nome non combacia)[/]")
            return
        res = await self.client.purge_profile(pname)
        self._status(f"[green]profilo '{pname}' rimosso[/]")
        self._result(self._dump(res))
        await self.app.refresh_data()

    async def _op_caps_audit(self) -> None:
        self._status("[dim]audit in corso…[/]")
        res = await self.client.capabilities_audit()
        miss = res.get("missing_models") or []
        self._status(f"[green]audit: {res.get('accounts_checked', '?')} "
                     f"account, {len(miss)} modelli mancanti[/]")
        self._result(self._dump(res))

    async def _op_caps_seed(self) -> None:
        self._status("[dim]anteprima dry-run…[/]")
        res = await self.client.capabilities_seed(dry_run=True)
        self._status("[green]anteprima pronta (dry_run)[/]")
        self._result(self._dump(res))

    async def _op_pressure_inspect(self) -> None:
        res = await self.client.pressure_inspect(limit=cfg.OPS_PRESSURE_LIMIT)
        cds = res.get("cooldowns") or []
        self._status(f"[green]{res.get('cooldowns_total', len(cds))} "
                     "cooldown/penalita attivi[/]")
        self._result(self._dump(res))

    async def _op_pressure_clear(self) -> None:
        target = await self._ask(
            "Azzera: vuoto = TUTTO · 'u:<unique>' · 'm:<model>'",
            placeholder="es. m:deepseek/deepseek-chat")
        if target is None:
            return
        unique = model = None
        if target.startswith("u:"):
            unique = target[2:]
        elif target.startswith("m:"):
            model = target[2:]
        res = await self.client.pressure_clear(unique=unique, model=model)
        self._status(f"[green]azzerati: {len(res.get('cleared') or [])}[/]")
        self._result(self._dump(res))

    async def _op_backups(self) -> None:
        res = await self.client.backups()
        csvs = backup_filenames(res.get("csv"))
        yamls = backup_filenames(res.get("yaml"))
        self._status(f"[green]{len(csvs)} CSV · {len(yamls)} YAML[/]")
        names = csvs + yamls
        _show = 25
        def _fmt(lst: list[str]) -> str:
            head = "\n  ".join(lst[:_show])
            if len(lst) > _show:
                head += f"\n  … (+{len(lst) - _show})"
            return head or "-"
        self._result("CSV:\n  " + _fmt(csvs) +
                     "\nYAML:\n  " + _fmt(yamls))
        if not names:
            return
        pick = await self._ask("Nome backup da RIPRISTINARE (vuoto = annulla)")
        if not pick:
            return
        if pick not in names:
            self._status(f"[yellow]'{pick}' non in elenco[/]")
            return
        if not await self._confirm(f"Ripristinare '{pick}'? Sovrascrive "
                                   "CSV/YAML correnti."):
            return
        res2 = await self.client.restore_backup(pick)
        self._status(f"[green]ripristinato: {pick}[/]")
        self._result(self._dump(res2))
        await self.app.refresh_data()

    async def _op_insights(self) -> None:
        days = await self._ask("Giorni", default="7")
        if not days:
            return
        try:
            days_i = int(days)
        except ValueError:
            self._status("[red]giorni non validi[/]")
            return
        group = await self._ask("Raggruppa per",
                                default="model",
                                placeholder="model|profile|deployment|day|kind|none")
        res = await self.client.insights(days_i, group or "model")
        self._status(f"[green]insights {days_i}g per {group or 'model'}[/]")
        self._result(self._dump(res))

    async def _op_history(self) -> None:
        limit = await self._ask("Quante voci (max 100)", default="50")
        if not limit:
            return
        try:
            n = min(100, int(limit))
        except ValueError:
            n = 50
        res = await self.client.history(n)
        entries = res.get("entries") or []
        self._status(f"[green]{res.get('total', len(entries))} voci[/]")
        self._result("\n".join(
            f"{e.get('ts')}  {e.get('action')}  {e.get('detail') or ''}"
            for e in entries[:cfg.OPS_HISTORY_MAX]))

    async def _op_guide(self) -> None:
        res = await self.client.guide()
        text = res.get("text") or ""
        self._status(f"[green]guida: {len(text)} caratteri[/]")
        self._result(text[:cfg.RESULT_MAX_CHARS])

    async def _op_playground(self) -> None:
        model = await self._ask("Modello da provare",
                                placeholder="es. deepseek/deepseek-chat")
        if not model:
            return
        prompt = await self._ask("Messaggio utente", default="ping")
        if prompt is None:
            return
        profile = self.app.current_profile()
        self._status("[dim]simulazione (read-only)…[/]")
        res = await self.client.playground(
            model, [{"role": "user", "content": prompt}], profile=profile)
        if res.get("error"):
            self._status(f"[red]{res['error']}[/]")
        else:
            self._status(f"[green]{res.get('attempts')} tentativi · "
                         f"gruppo {res.get('group')}[/]")
        self._result(self._dump(res))
