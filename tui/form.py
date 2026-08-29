"""Form di creazione/modifica deployment (modale Textual)."""
from __future__ import annotations

from typing import Any

from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Label, Select

CATEGORIES = [("free", "free"), ("priority", "priority"),
              ("fallback", "fallback"), ("paid", "paid")]
CATEGORIES += [(f"giorno {d:02d} (rinnovo)", str(d)) for d in range(1, 32)]

# token caps nell'ordine fisso usato per serializzare il payload
CAP_TOKENS = ["text", "vision", "video", "audio",
              "image_gen", "tts", "stt"]


class DeploymentFormScreen(ModalScreen[dict | None]):
    """Create (dep=None) o edit (dep=view dict). Ritorna payload dict o None.

    Il payload usa i NOMI delle API admin: profile/modello/provider/endpoint/
    data/context/max_input/priority/key. In edit i campi vuoti NON vengono
    inviati (merge parziale server-side).
    """

    BINDINGS = [Binding("ctrl+s", "save", "Salva"),
                Binding("escape", "cancel", "Annulla")]

    def __init__(self, profiles: list[str], dep: dict | None = None,
                 preset_profile: str | None = None):
        super().__init__()
        self.profiles = profiles or ["collego"]
        self.dep = dep
        self.preset_profile = preset_profile

    def compose(self):
        d = self.dep or {}
        prof = d.get("profile") or self.preset_profile or self.profiles[0]
        data_val = str(d.get("data") or "free")
        day_default = data_val if data_val.isdigit() else ""
        cat_default = data_val if not data_val.isdigit() else "free"
        title = ("Modifica deployment [bold cyan]"
                 f"{str(d.get('id') or '')[:18]}[/]" if d else "Nuovo deployment")
        with VerticalScroll(id="form-box"):
            yield Label(title, id="form-title")
            yield Input(value=str(prof), placeholder="profilo *",
                        id="f_profile")
            yield Input(value=d.get("modello") or "", placeholder="modello *",
                        id="f_modello")
            yield Input(value=d.get("provider") or "", id="f_provider",
                        placeholder="provider (groq/mistral/opencode-go…)")
            yield Input(value=d.get("endpoint") or "", id="f_endpoint",
                        placeholder="endpoint https://… *")
            yield Select(CATEGORIES, prompt="categoria/data *",
                         value=cat_default if cat_default in
                         {v for _l, v in CATEGORIES} else "free",
                         id="f_cat", allow_blank=False)
            yield Input(value=day_default, id="f_day",
                        placeholder="giorno rinnovo 1-31 (solo se categoria=giorno)")
            ctx = d.get("context_k")
            yield Input(value="" if ctx is None else str(ctx), id="f_ctx",
                        placeholder="context in migliaia * (32 / 200 / 1000)")
            mx = d.get("max_input")
            yield Input(value="" if not mx else str(mx), id="f_maxin",
                        placeholder="max_input token (vuoto = ctx×1000)")
            prio = d.get("priority")
            yield Input(value="0" if prio is None else str(prio),
                        id="f_prio", placeholder="priority (peso selezione)")
            yield Label("Capacità (caps)", id="form-caps-title")
            with Horizontal(id="form-caps"):
                for tok in CAP_TOKENS:
                    yield Checkbox(tok, id=f"f_cap_{tok}")
            key_ph = ("nuova chiave (VUOTO = lascia invariata)" if d
                      else "chiave API *")
            yield Input(value="", placeholder=key_ph, id="f_key",
                        password=True)
            yield Checkbox("mostra chiave", id="f_show")
            yield Label("", id="form-error")
            yield Label("[dim]ctrl+s salva · esc annulla[/dim]", id="hint")

    def on_mount(self) -> None:
        # pre-spunta le caps del deployment in modifica (lista token API)
        caps = (self.dep or {}).get("caps")
        if isinstance(caps, list):
            have = {str(c).strip() for c in caps}
            for tok in CAP_TOKENS:
                self.query_one(f"#f_cap_{tok}", Checkbox).value = tok in have
        self.query_one("#f_profile", Input).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "f_show":   # le caps non toccano la chiave
            return
        self.query_one("#f_key", Input).password = not event.value

    @staticmethod
    def _int(v: str, what: str, required: bool = False) -> int | None:
        v = (v or "").strip()
        if not v:
            if required:
                raise ValueError(f"'{what}' obbligatorio")
            return None
        try:
            return int(float(v))
        except ValueError:
            raise ValueError(f"'{what}' deve essere un intero") from None

    def collect(self) -> dict:
        def g(wid: str) -> str:
            return self.query_one(wid, Input).value.strip()

        is_edit = self.dep is not None
        out: dict[str, Any] = {}
        # --- campi sempre gestiti (create) o solo se modificati (edit)
        prof = g("#f_profile") or (self.dep or {}).get("profile", "")
        if not is_edit or prof != (self.dep or {}).get("profile"):
            out["profile"] = prof
        modello = g("#f_modello")
        if modello and (not is_edit or modello != (self.dep or {}).get("modello")):
            out["modello"] = modello
        provider = g("#f_provider")
        if provider and (not is_edit or provider != (self.dep or {}).get("provider")):
            out["provider"] = provider
        endpoint = g("#f_endpoint")
        if endpoint and (not is_edit or endpoint != (self.dep or {}).get("endpoint")):
            out["endpoint"] = endpoint
        # categoria/data: giorno del mese ha priorità sul campo libero
        day = g("#f_day")
        cat = self.query_one("#f_cat", Select).value or "free"
        data_val = day if day.isdigit() else str(cat)
        if data_val != str((self.dep or {}).get("data")):
            out["data"] = data_val
        ctx = self._int(g("#f_ctx"), "context", required=not is_edit)
        if ctx is not None and (not is_edit or ctx != (self.dep or {}).get("context_k")):
            out["context"] = ctx
        maxin = self._int(g("#f_maxin"), "max_input")
        if maxin is not None:
            out["max_input"] = maxin
        prio = self._int(g("#f_prio"), "priority") or 0
        if not is_edit or prio != (self.dep or {}).get("priority"):
            out["priority"] = prio
        # caps: SEMPRE inviate come stringa CSV nell'ordine fisso
        # ("" = rimozione esplicita della membership, il backend la tratta
        # come lista vuota)
        out["caps"] = ",".join(tok for tok in CAP_TOKENS
                               if self.query_one(f"#f_cap_{tok}",
                                                 Checkbox).value)
        key = g("#f_key")
        if key:
            if len(key) < 8:
                raise ValueError("'key' deve avere almeno 8 caratteri")
            out["key"] = key
        elif not is_edit:
            raise ValueError("'key' obbligatoria per la creazione")

        # validazione minima speculare al server (per messaggi chiari)
        if not is_edit:
            missing = [f for f in ("profile", "modello", "endpoint", "data")
                       if not out.get(f)]
            if missing:
                raise ValueError(f"campi obbligatori mancanti: {missing}")
        return out

    def action_save(self) -> None:
        err = self.query_one("#form-error", Label)
        try:
            payload = self.collect()
        except ValueError as exc:
            err.update(f"[red]{exc}[/]")
            return
        if self.dep is not None:
            payload["_edit_id"] = self.dep.get("id")
        self.dismiss(payload)

    def action_cancel(self) -> None:
        self.dismiss(None)
