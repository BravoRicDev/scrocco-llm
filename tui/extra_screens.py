"""Schermate secondarie: chiavi client, rotazioni, capacità modalità (M)."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label

from .gateway_client import GatewayClient, GatewayError


class CapacitiesScreen(ModalScreen[None]):
    """Riepilogo capacità modalità: per_capability, strike auto-learn,
    contatori audio e stato health proattivo (da GET /admin/state)."""

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("r", "refresh", "Ricarica")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client

    def compose(self):
        with VerticalScroll(id="modal-box"):
            yield Label("[b cyan]CAPACITÀ MODALITÀ[/b] · [dim]r ricarica · "
                        "esc chiudi[/]", id="form-title")
            yield Label("", id="caps-health")
            yield DataTable(cursor_type="none", zebra_stripes=True,
                            id="caps-t")
            yield DataTable(cursor_type="none", zebra_stripes=True,
                            id="caps-groups")
            yield Label("", id="caps-fallback")
            yield Label("", id="caps-counters")
            yield Label("", id="caps-strikes")

    def on_mount(self) -> None:
        t = self.query_one("#caps-t", DataTable)
        t.add_columns("Capacità", "Deployment")
        gt = self.query_one("#caps-groups", DataTable)
        gt.add_columns("Capacità", "primary", "go", "fallback")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        health_lbl = self.query_one("#caps-health", Label)
        fallback_lbl = self.query_one("#caps-fallback", Label)
        counters_lbl = self.query_one("#caps-counters", Label)
        strikes_lbl = self.query_one("#caps-strikes", Label)
        try:
            st = await self.client.state()
        except GatewayError as exc:
            health_lbl.update(f"[red]state: {exc.message}[/]")
            return
        caps = st.get("capabilities") or {}
        h = st.get("health") or {}

        def _fmt_ts(ts) -> str:
            if not ts:
                return "mai"
            from datetime import datetime
            return datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")

        health_lbl.update(
            f"[dim]routing:[/] {'[green]ON[/]' if caps.get('routing_enabled') else '[red]OFF[/]'}"
            f"  ·  [dim]health proattivo:[/] {h.get('enabled', False)}"
            f" ({_fmt_ts(h.get('last_cycle_at'))}, marcati={h.get('marked', 0)},"
            f" account={h.get('accounts', 0)})")

        t = self.query_one("#caps-t", DataTable)
        t.clear()
        for cap, n in (caps.get("per_capability") or {}).items():
            badge = {"vision": "V immagini", "video": "D video",
                     "audio": "A audio-chat", "image_gen": "I generazione",
                     "tools": "T tools", "tts": "P speech",
                     "stt": "S trascrizione", "text": "testo"}.get(cap, cap)
            t.add_row(f"{cap}", f"{n}  [dim]{badge}[/]")

        def _n(v) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        # gruppi capacità del profilo corrente: cap -> {primary, go, fallback}
        gt = self.query_one("#caps-groups", DataTable)
        gt.clear()
        groups = ((caps.get("groups") or {})
                  .get(self.app.current_profile() or "") or {})
        if isinstance(groups, dict):
            for cap in sorted(groups):
                info = groups.get(cap) or {}
                if not isinstance(info, dict):
                    continue
                gt.add_row(str(cap), str(_n(info.get("primary"))),
                           str(_n(info.get("go"))),
                           str(_n(info.get("fallback"))))

        # fallback per capacità sul profilo corrente ({cap: [univoci]})
        fb = (caps.get("fallback") or {}).get(self.app.current_profile() or "")
        if not isinstance(fb, dict):
            fb = {}
        if fb:
            lines = ["[b]Fallback per capacità[/b] [dim](catena del profilo: "
                     "automatiche usano tutta la catena, esplicite ruotano nel "
                     "gruppo)[/]"]
            order = ["vision", "video", "audio", "image_gen", "tts", "stt",
                     "tools"]
            for cap in order:
                chain = fb.get(cap) or []
                if not chain:
                    lines.append(f"  · [red]{cap}: NESSUN deployment[/]")
                    continue
                short = ", ".join(u.rsplit("__", 1)[-1][:22] for u in chain)
                lines.append(f"  · [cyan]{cap}:[/] {short}")
            fallback_lbl.update("\n".join(lines))
        else:
            fallback_lbl.update("[dim]nessun profilo selezionato[/]")

        cnt = caps.get("counters") or {}
        unroutable = cnt.get("nx_caps_unroutable_total") or {}
        tts_c = cnt.get("nx_tts_total") or {}
        stt_c = cnt.get("nx_stt_total") or {}
        counters_lbl.update(
            f"[dim]unroutable:[/] {sum(unroutable.values()) or 0}"
            f"   [dim]tts ok:[/] {tts_c.get('ok', 0)}"
            f"   [dim]stt ok:[/] {stt_c.get('ok', 0)}")

        al = (caps.get("auto_learn") or {})
        strikes = al.get("strikes") or []
        if strikes:
            lines = ["[yellow]strike auto-learn[/] "
                     f"(mode={al.get('mode')}, soglia={al.get('threshold')}):"]
            for s in strikes[:8]:
                lines.append(f"  · {s['model']} [{s['cap']}] ×{s['count']}"
                             f" — {(s.get('evidence') or '')[:70]}")
            if len(strikes) > 8:
                lines.append(f"  … +{len(strikes) - 8}")
            strikes_lbl.update("\n".join(lines))
        else:
            strikes_lbl.update("[dim]nessuno strike auto-learn[/]")

    def action_close(self) -> None:
        self.dismiss(None)


class ClientKeysScreen(ModalScreen[None]):
    """Chiavi CLIENT deterministiche (sk-<profilo>) + master mascherata.

    Nel nuovo gateway le chiavi si DEDUCONO: non serve crearle né ruotarle.
    """

    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("c", "copy_hint", "Copia", show=False)]

    def __init__(self, profiles: list[str], master_masked: str,
                 proxy_prefix: str):
        super().__init__()
        self.profiles = profiles
        self.master_masked = master_masked or "(non configurata)"
        self.proxy_prefix = proxy_prefix or ""

    def compose(self):
        with Vertical(id="modal-box keys-box"):
            yield Label("[b cyan]Chiavi client[/b] · [dim]deterministiche: "
                        "sk-<profilo>[/]", id="form-title")
            yield DataTable(cursor_type="row", zebra_stripes=True, id="keys-t")
            yield Label(f"[b]master[/b]: {self.master_masked}   "
                        f"[dim](admin: header Bearer)[/]", id="hint")

    def on_mount(self) -> None:
        t = self.query_one("#keys-t", DataTable)
        t.add_columns("Profilo", "Chiave client", "Base model")
        for p in self.profiles:
            t.add_row(p, f"sk-{p}", f"{self.proxy_prefix}{p}")
        t.focus()

    def action_close(self) -> None:
        self.dismiss(None)


class ExpiringScreen(ModalScreen[None]):
    """Rotazioni in arrivo entro N giorni (colonna data = giorno del mese)."""

    BINDINGS = [Binding("escape", "close", "Chiudi")]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client

    def compose(self):
        with VerticalScroll(id="modal-box"):
            yield Label("[b cyan]Rotazioni in arrivo[/b] · [dim]entro N "
                        "giorni[/]", id="form-title")
            yield Input(value="7", placeholder="giorni (invio aggiorna)",
                        id="exp-days")
            yield Label("", id="exp-status")
            yield DataTable(cursor_type="row", zebra_stripes=True, id="exp-t")
            yield Label("[dim]esc = chiudi[/dim]", id="hint")

    def on_mount(self) -> None:
        t = self.query_one("#exp-t", DataTable)
        t.add_columns("Giorni", "Modello", "data raw", "id")
        self.run_worker(self._load(7), exclusive=True)
        self.query_one("#exp-days", Input).focus()

    async def _load(self, days: int) -> None:
        if getattr(self, "_loading", False):    # ignora invii doppi rapidi
            return
        self._loading = True
        status = self.query_one("#exp-status", Label)
        try:
            rows = await self.client.expiring(days)
        except GatewayError as exc:
            status.update(f"[red]{exc.message}[/]")
            return
        finally:
            self._loading = False
        t = self.query_one("#exp-t", DataTable)
        t.clear()
        for r in rows:
            t.add_row(str(r.get("in_days")), r.get("modello") or "?",
                      str(r.get("data_raw") or ""), str(r.get("id")))
        status.update(f"[green]{len(rows)} rinnovi entro {days} giorni[/]")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "exp-days":
            return
        try:
            days = max(0, int(event.value.strip() or "7"))
        except ValueError:
            self.app.notify("inserisci un numero di giorni", severity="error")
            return
        await self._load(days)

    def action_close(self) -> None:
        self.dismiss(None)
