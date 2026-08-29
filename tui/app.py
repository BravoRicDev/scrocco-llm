"""GatewayTUI — interfaccia Textual di gestione completa di scrocco-llm.

Layout fedele al vecchio installer: profili a sinistra, deployment a destra,
header di stato, filtro rapido. Tutto via admin API (mai tocco al CSV).
"""
from __future__ import annotations

import asyncio
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .extra_screens import CapacitiesScreen, ClientKeysScreen, ExpiringScreen
from .observability import ObservabilityScreen
from .form import DeploymentFormScreen
from .gateway_client import DEFAULT_BASE, GatewayClient, GatewayError
from .modals import (ConfirmModal, HelpScreen, InfoModal, TextInputModal,
                     TypedConfirmModal)
from .policy_screen import AdvancedPolicyScreen

DEP_COLUMNS = [
    ("id", 13), ("modello", 24), ("provider", 11), ("data", 9),
    ("ctx", 7), ("prio", 5), ("endpoint", 34), ("chiave", 12),
    ("gruppo", 22), ("caps", 14),
]


class MainScreen(Screen):
    BINDINGS = [
        Binding("q", "quit_app", "Esci"),
        Binding("r", "refresh", "Ricarica"),
        Binding("R", "remote_reload", "Reload gw."),
        Binding("n", "new_dep", "Nuovo"),
        Binding("e", "edit_dep", "Modifica"),
        Binding("d", "del_dep", "Elimina"),
        Binding("p", "new_profile", "Nuovo profilo"),
        Binding("x", "del_profile", "Del profilo"),
        Binding("k", "client_keys", "Chiavi"),
        Binding("Y", "advanced", "Avanzate"),
        Binding("m", "capacities", "Capacità"),
        Binding("E", "expiring", "Scadenze"),
        Binding("C", "clear_cooldowns", "Sblocca cd"),
        Binding("O", "observability", "Osserva"),
        Binding("S", "release_sessions", "Rilascia sess."),
        Binding("slash", "filter_focus", "Filtra", key_display="/"),
        Binding("question_mark", "help", "Aiuto", key_display="?"),
    ]

    def __init__(self):
        super().__init__()
        self.filter_text = ""
        self.visible_deps: list[dict] = []

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Static("", id="hdr")
        with Horizontal(id="body"):
            yield OptionList(id="profiles")
            yield DataTable(id="deps", cursor_type="row",
                            zebra_stripes=True)
        yield Input(id="filter",
                    placeholder="/ filtra modello·provider·endpoint·gruppo "
                                "(invio torna alla tabella · esc pulisce)")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#deps", DataTable)
        for label, width in DEP_COLUMNS:
            t.add_column(label, key=label, width=width)
        self.run_worker(self.app.refresh_data(), exclusive=False)

    # ------------------------------------------------------------- helpers
    def current_profile(self) -> str | None:
        profs = self.app.profiles
        if not profs:
            return None
        return profs[min(self.app.prof_idx, len(profs) - 1)]

    def selected_dep(self) -> dict | None:
        t = self.query_one("#deps", DataTable)
        if not t.row_count or t.cursor_row is None:
            return None
        if 0 <= t.cursor_row < len(self.visible_deps):
            return self.visible_deps[t.cursor_row]
        return None

    # ------------------------------------------------------------- render
    def render_header(self) -> None:
        hdr = self.query_one("#hdr", Static)
        app = self.app
        line = "[b black on cyan] Gateway Installer [/]  "
        line += f"[dim]servizio:[/] [b]{app.service_name}[/]   "
        line += f"[dim]profilo:[/] [b cyan]{self.current_profile() or '—'}[/]"
        if app.cooldown_count or app.sticky_count:
            line += (f"   [yellow]cd:{app.cooldown_count}"
                     f" sticky:{app.sticky_count}[/]")
        if self.filter_text:
            line += f'   [dim italic]filtro: "{self.filter_text}"[/]'
        if app.last_refresh:
            from datetime import datetime as _dt
            age = time.time() - app.last_refresh
            if age < 2:
                ago = "ora"
            elif age < 60:
                ago = f"{int(age)}s fa"
            else:
                ago = f"{int(age // 60)}min fa"
            line += f"   [dim]aggiornato:[/] {_dt.fromtimestamp(app.last_refresh).strftime('%H:%M:%S')} ({ago})"
        hdr.update(line)

    def render_profiles(self) -> None:
        ol = self.query_one("#profiles", OptionList)
        ol.clear_options()
        counts = self.app.profile_counts
        for i, p in enumerate(self.app.profiles):
            marker = "▸ " if i == min(self.app.prof_idx,
                                      len(self.app.profiles) - 1) else "  "
            ol.add_option(Option(f"{marker}{p} ({counts.get(p, 0)})",
                                 id=str(i)))
        ol.add_option(Option("+ nuovo profilo [p]", id="__new__"))
        if self.app.profiles:
            ol.highlighted = min(self.app.prof_idx,
                                 len(self.app.profiles) - 1)

    def render_table(self) -> None:
        t = self.query_one("#deps", DataTable)
        t.clear()
        self.visible_deps = []
        f = self.filter_text.lower()
        for dep in self.app.deps:
            # caps dichiarate (nuovo contratto): lista, tollera stringa CSV
            raw_caps = dep.get("caps")
            if isinstance(raw_caps, str):
                dep_caps = [c.strip() for c in raw_caps.split(",")
                            if c.strip()]
            elif isinstance(raw_caps, list):
                dep_caps = [str(c) for c in raw_caps]
            else:
                dep_caps = []
            if f and f not in str(dep.get("modello", "")).lower() \
                    and f not in str(dep.get("provider", "")).lower() \
                    and f not in str(dep.get("endpoint", "")).lower() \
                    and f not in str(dep.get("group", "")).lower() \
                    and f not in ",".join(dep_caps) \
                    and f not in ",".join(dep.get("capabilities") or []):
                continue
            self.visible_deps.append(dep)
            # badge: PRIMA la membership reale ("caps"), altrimenti le
            # capabilities risolte; iniziali delle capacità extra oltre a text
            caps = dep_caps or list(dep.get("capabilities") or [])
            badge = "".join({"vision": "V", "video": "D", "audio": "A",
                             "image_gen": "I", "tools": "T",
                             "stt": "S", "tts": "P", "video_gen": "G"}.get(c, "")
                            for c in caps if c != "text")
            t.add_row(
                str(dep.get("id", ""))[:13],
                str(dep.get("modello", ""))[:24],
                str(dep.get("provider", ""))[:11],
                str(dep.get("data", ""))[:9],
                str(dep.get("context_k") or ""),
                str(dep.get("priority") or 0),
                str(dep.get("endpoint", ""))[:34],
                str(dep.get("key_masked", "")),
                str(dep.get("group", ""))[:22],
                (f"[cyan]{badge}[/]" if badge else "[dim]text[/]"),
                key=str(dep.get("id")),
            )
        self.render_header()

    # ------------------------------------------------------------- eventi
    def on_option_list_option_selected(self, event) -> None:
        opt_id = str(event.option.id)
        if opt_id == "__new__":
            self.action_new_profile()
            return
        self.app.prof_idx = int(opt_id)
        self.render_profiles()
        self.run_worker(self.app.load_deps(), exclusive=True)

    def on_data_table_row_selected(self, event) -> None:
        self.action_edit_dep()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        self.filter_text = event.value.strip()
        self.render_table()
        self.query_one("#deps", DataTable).focus()

    def on_key(self, event) -> None:
        if event.key == "escape" and \
                self.app.focused is not None and \
                getattr(self.app.focused, "id", "") == "filter":
            flt = self.query_one("#filter", Input)
            flt.value = ""
            self.filter_text = ""
            self.render_table()
            self.query_one("#deps", DataTable).focus()

    # ------------------------------------------------------------- azioni
    def action_quit_app(self) -> None:
        self.app.exit()

    def action_refresh(self) -> None:
        self.run_worker(self.app.refresh_data(), exclusive=True)

    def action_remote_reload(self) -> None:
        self.run_worker(self._remote_reload(), exclusive=True)

    async def _remote_reload(self) -> None:
        try:
            data = await self.app.client.reload()
        except GatewayError as exc:
            self.app.notify(exc.message, severity="error")
            return
        self.app.notify(f"gateway ricaricato: {data.get('deployments')} "
                        "deployment")

    def action_clear_cooldowns(self) -> None:
        self.run_worker(self._clear_cooldowns(), exclusive=True)

    async def _clear_cooldowns(self) -> None:
        try:
            res = await self.app.client.clear_cooldowns()
        except GatewayError as exc:
            self.app.notify(exc.message, severity="error")
            return
        self.app.notify(f"cooldown sbloccati: {len(res.get('cleared', []))}")
        await self.app.refresh_data()

    def action_release_sessions(self) -> None:
        self.run_worker(self._release_sessions(), exclusive=True)

    async def _release_sessions(self) -> None:
        try:
            res = await self.app.client.release_sessions()
        except GatewayError as exc:
            self.app.notify(exc.message, severity="error")
            return
        self.app.notify(f"sessioni rilasciate: {len(res.get('released', []))}")

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_client_keys(self) -> None:
        self.app.push_screen(ClientKeysScreen(
            self.app.profiles, self.app.master_masked,
            self.app.proxy_prefix))

    def action_advanced(self) -> None:
        self.app.push_screen(AdvancedPolicyScreen(self.app.client))

    def action_capacities(self) -> None:
        self.app.push_screen(CapacitiesScreen(self.app.client))

    def action_expiring(self) -> None:
        self.app.push_screen(ExpiringScreen(self.app.client))

    def action_observability(self) -> None:
        self.app.push_screen(ObservabilityScreen(self.app.client))

    def action_filter_focus(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_new_profile(self) -> None:
        self.run_worker(self._new_profile_flow(), exclusive=True)

    async def _new_profile_flow(self) -> None:
        name = await self.app.push_screen_wait(TextInputModal(
            "[b]Nuovo profilo[/b]\n[dim]verrà creato col primo deployment "
            "(colonna on-demand)[/]", placeholder="es. secondo"))
        if not name:
            return
        await self._open_form(preset_profile=name)

    def action_del_profile(self) -> None:
        self.run_worker(self._del_profile_flow(), exclusive=True)

    async def _del_profile_flow(self) -> None:
        pname = self.current_profile()
        if not pname:
            self.app.notify("nessun profilo da eliminare",
                            severity="warning")
            return
        ok = await self.app.push_screen_wait(TypedConfirmModal(
            f"Eliminare il PROFILO '{pname}' e TUTTI i suoi deployment?",
            pname))
        if not ok:
            return
        try:
            deps = await self.app.client.deployments(pname)
            ops = [{"action": "delete", "id": d["id"]} for d in deps]
            if ops:
                await self.app.client.bulk(ops)
        except GatewayError as exc:
            self.app.notify(exc.message, severity="error")
            return
        self.app.prof_idx = 0
        self.app.notify(f"profilo '{pname}' eliminato ({len(ops)} dep.)")
        await self.app.refresh_data()

    def action_new_dep(self) -> None:
        self.run_worker(self._open_form(), exclusive=True)

    async def _open_form(self, preset_profile: str | None = None) -> None:
        payload = await self.app.push_screen_wait(DeploymentFormScreen(
            self.app.profiles, None, preset_profile=preset_profile))
        if not payload:
            return
        payload.pop("_edit_id", None)
        try:
            await self.app.client.create_deployment(payload)
        except GatewayError as exc:
            await self.app.push_screen_wait(InfoModal(str(exc), error=True))
            return
        self.app.notify("deployment creato")
        await self.app.refresh_data()
        if preset_profile:
            for i, p in enumerate(self.app.profiles):
                if p == preset_profile:
                    self.app.prof_idx = i

    def action_edit_dep(self) -> None:
        self.run_worker(self._edit_flow(), exclusive=True)

    async def _edit_flow(self) -> None:
        dep = self.selected_dep()
        if not dep:
            self.app.notify("seleziona un deployment", severity="warning")
            return
        payload = await self.app.push_screen_wait(DeploymentFormScreen(
            self.app.profiles, dict(dep)))
        if not payload:
            return
        dep_id = payload.pop("_edit_id", None) or dep.get("id")
        if "context" in payload and payload["context"] is None:
            payload.pop("context")
        try:
            await self.app.client.update_deployment(str(dep_id), payload)
        except GatewayError as exc:
            await self.app.push_screen_wait(InfoModal(str(exc), error=True))
            return
        self.app.notify("deployment aggiornato (nuovo id se ruotata la chiave)")
        await self.app.refresh_data()

    def action_del_dep(self) -> None:
        self.run_worker(self._del_dep_flow(), exclusive=True)

    async def _del_dep_flow(self) -> None:
        dep = self.selected_dep()
        if not dep:
            self.app.notify("seleziona un deployment", severity="warning")
            return
        ok = await self.app.push_screen_wait(ConfirmModal(
            f"Eliminare [b]{dep.get('modello')}[/b] "
            f"({str(dep.get('id'))[:16]})?"))
        if not ok:
            return
        try:
            await self.app.client.delete_deployment(str(dep["id"]))
        except GatewayError as exc:
            self.app.notify(exc.message, severity="error")
            return
        self.app.notify("deployment eliminato")
        await self.app.refresh_data()


class GatewayTUI(App[None]):
    TITLE = "scrocco-llm · Gateway Installer"

    CSS = """
#hdr { dock: top; height: 1; padding: 0 1; background: $surface; }
#body { height: 1fr; }
#profiles { width: 30; border: round $accent; margin-right: 1; }
#deps { border: round $accent-lighten-1; }
#filter { dock: bottom; }
ModalScreen { align: center middle; }
#modal-box, #form-box, #help-box, #adv-box {
    width: auto; max-width: 92%; padding: 1 2;
    border: round $accent; background: $surface; }
.modal-error { border: round $error; }
#help-box, #adv-box { width: 88%; }
.keys-box { width: 72; }
#form-title { text-style: bold; color: $accent; margin-bottom: 1; }
#hint { color: $text-muted; margin-top: 1; }
#form-error { color: $error; }
Select { margin-bottom: 1; }
Input { margin-bottom: 1; }
"""

    BINDINGS = [Binding("ctrl+c", "quit", "Esci", priority=True)]

    def __init__(self, base_url: str | None = None,
                 master_key: str | None = None, driver=None):
        super().__init__()
        self.client = GatewayClient(base_url=base_url, master_key=master_key)
        self.base_url = self.client.base_url
        self.master_key = self.client.master_key
        self.profiles: list[str] = []
        self.deps: list[dict] = []
        self.profile_counts: dict[str, int] = {}
        self.prof_idx = 0
        self.service_name = "…"
        self.proxy_prefix = ""
        self.master_masked = "***"
        self.cooldown_count = 0
        self.sticky_count = 0
        self.last_refresh: float | None = None

    def on_mount(self) -> None:
        self.push_screen(MainScreen())

    def current_profile(self) -> str | None:
        """Profilo attivo: delegato al MainScreen (anche sotto una modale)."""
        for scr in reversed(self.screen_stack):
            if isinstance(scr, MainScreen):
                return scr.current_profile()
        return None

    async def on_unmount(self) -> None:
        """Chiude il client HTTP (evita connessioni orfane a shutdown)."""
        try:
            await self.client.aclose()
        except Exception:               # mai bloccare l'uscita
            pass

    async def refresh_data(self) -> None:
        """Stato completo: profili+conteggi, state, deployments del profilo."""
        main = self.screen if isinstance(self.screen, MainScreen) else None
        try:
            state = await self.client.state()
            # il server espone 'service' (vedi GET /admin/state)
            self.service_name = state.get("service",
                                          state.get("service_name", "?"))
            self.proxy_prefix = state.get("prefix", "")
            self.cooldown_count = len(state.get("cooldowns_active") or [])
            self.sticky_count = len(state.get("sticky_sessions") or [])
            profs = await self.client.profiles()
            self.profile_counts = {p["name"]: p.get("deployments", 0)
                                   for p in profs}
            self.profiles = [p["name"] for p in profs]
            self.master_masked = (f"{self.master_key[:8]}…"
                                  if len(self.master_key) > 12 else "***")
            await self.load_deps()
            self.last_refresh = time.time()
        except GatewayError as exc:
            self.notify(f"gateway non raggiungibile ({exc.status}): "
                        f"{exc.message}", severity="error")
        if main is not None:
            main.render_profiles()
            main.render_table()

    async def load_deps(self) -> None:
        main = self.screen if isinstance(self.screen, MainScreen) else None
        try:
            self.deps = await self.client.deployments(
                main.current_profile() if main else None)
        except GatewayError as exc:
            self.notify(exc.message, severity="error")
            return
        if main is not None:
            main.render_table()


def main() -> None:
    GatewayTUI().run()


if __name__ == "__main__":
    main()
