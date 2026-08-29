"""Pagina AVANZATE: tutti i parametri della policy, editabili al volo.

Copre: prefisso/suffissi naming, soglia di SALITA globale e PER PROFILO,
alias (con prefisso nel target), timing (sticky/cooldown/divisor/window),
legacy prefixes e hotword. Ogni modifica è un PATCH atomico validato dal
gateway (yaml invalido => rifiutato senza toccare nulla).
"""
from __future__ import annotations

from typing import Any

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

from .gateway_client import GatewayClient, GatewayError
from .modals import TextInputModal

# (sezione, chiave-patch, etichetta, tipo)
SCALAR_ROWS: list[tuple[str, str, str, str]] = [
    ("Generali", "service_name", "Nome servizio", "str"),
    ("Generali", "proxy_prefix", "PREFISSO modelli", "str"),
    ("Generali", "go_suffix", "Suffisso gruppo -go", "str"),
    ("Generali", "fallback_suffix", "Suffisso -fallback", "str"),
    ("Risposta", "response_model",
     'Campo "model" risposta (requested|deployment|upstream)', "str"),
    ("Routing", "step_up_pct", "Soglia SALITA globale (%)", "int"),
    ("Routing", "speed_min_dim_k",
      "Contesto minimo gruppo VELOCE (k)", "int"),
    ("Routing", "speed_qualify_pct", "Margine fit scelta VELOCE (%)", "int"),
    ("Routing", "estimate_divisor", "Stima token: chars/token", "int"),
    ("Adattivo", "adaptive_pick",
     "Rotazione adattiva (true/false)", "bool"),
    ("Adattivo", "recency_halflife_sec",
     "Half-life recency ultimo uso (s)", "num"),
    ("Adattivo", "latency_ref_ms", "Latenza di riferimento (ms)", "num"),
    ("Timing", "sticky_ttl_sec", "Sticky session TTL (s)", "int"),
    ("Timing", "cooldown_sec", "Cooldown dopo fallimento (s)", "int"),
    ("Timing", "hotwords_window", "Finestra hot-word (msg)", "int"),
    ("Timing", "cooldown_escalation",
     "Escalation cooldown x2 a ogni fallimento (true/false)", "bool"),
    ("Timing", "max_cooldown_sec", "Cooldown MASSIMO escalation (s)", "int"),
    ("Health", "proactive_health",
     "Health proattivo GET /models (true/false)", "bool"),
    # --- capacità modalità (chiavi annidate sotto capability_routing.*) ---
    ("Capacità", "capability_routing.enabled",
     "Routing per capacità vision/video/audio/tts/stt (true/false)", "bool"),
    ("Capacità", "capability_routing.images_chat_fallback",
     "Images: fallback via chat se /images assente (true/false)", "bool"),
    ("Capacità", "capability_routing.image_token_estimate",
     "Token stimati per IMMAGINE nel contesto (0 = ignora)", "int"),
    ("Capacità", "capability_routing.auto_learn",
     "Auto-learn rifiuti modalità (off|suggest|auto)", "cap_mode"),
    ("Capacità", "capability_routing.auto_learn_threshold",
     "STRIKE prima della rimozione automatica capacità", "int"),
    ("Cap-gruppi", "capability_groups.enabled",
     "Gruppi capacità strutturali -C (true/false, DOPO il seed)", "bool"),
    ("Cap-gruppi", "capability_groups.on_missing",
     "Cap senza gruppo: dynamic (filtro legacy) | error (400)", "cap_missing"),
    ("QC", "qc_sanity.enabled",
     "Sanity QC: scarta risposte VUOTE non-streaming (true/false)", "bool"),
    ("QC", "qc_sanity.min_chars",
     "Sanity QC: min caratteri contenuti (0 = solo null)", "int"),
]

# liste (virgola) — valori validati dal gateway al PATCH
LIST_ROWS: list[tuple[str, str, str]] = [
    ("Compatibilità", "legacy_prefixes",
     "Prefissi storici accettati (virgola)"),
    ("Hot-word", "hotwords",
     "Regex hot-word RAGIONE (separate da |)"),
    ("Hot-word", "speed_hotwords",
     "Regex hot-word VELOCITÀ (separate da |)"),
]

MAP_ROWS: list[tuple[str, str, str]] = [
    ("Capacità", "capability_routing.model_capabilities",
     "Pattern→capacità · formato: glob:cap,cap ; separati da ;"),
]


class AdvancedPolicyScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("e", "edit_row", "Modifica"),
        Binding("a", "add_alias", "Nuovo alias"),
        Binding("D", "del_alias", "Elimina alias"),
        Binding("k", "alias_key", "Chiave alias"),
        Binding("escape", "close", "Chiudi"),
        Binding("enter", "close", "Chiudi", show=False),
    ]

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self.effective: dict[str, Any] = {}
        self.configured: dict[str, Any] = {}
        self._row_meta: dict[str, tuple] = {}   # row_key -> meta

    def compose(self):
        with Vertical(id="adv-box"):
            yield Label("[b cyan]AVANZATE[/] · [dim]e modifica · "
                        "a nuovo alias · D elimina alias · k chiave alias · "
                        "esc chiudi[/]", id="form-title")
            yield DataTable(cursor_type="row", zebra_stripes=True,
                            id="pol-table")

    async def on_mount(self) -> None:
        t = self.query_one("#pol-table", DataTable)
        t.add_columns("Sezione", "Parametro", "Valore")
        await self.reload()

    async def reload(self) -> None:
        try:
            data = await self.client.policy_get()
        except GatewayError as exc:
            self.app.notify(f"lettura policy: {exc.message}",
                            severity="error")
            return
        self.effective = data.get("effective") or {}
        self.configured = data.get("configured") or {}
        self._render_table()

    def _fmt(self, v: Any) -> str:
        if isinstance(v, list):
            return " | ".join(str(x) for x in v) or "(vuoto)"
        if isinstance(v, dict):
            return ", ".join(f"{k}→{x}" for k, x in v.items()) or "(nessuno)"
        if isinstance(v, bool):
            return "true" if v else "false"
        return "" if v is None else str(v)

    def _eff_get(self, dotted: str) -> Any:
        """Lettura annidata nell'effective: 'capability_routing.enabled'."""
        cur: Any = self.effective
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    @staticmethod
    def _nested_patch(dotted: str, value: Any) -> dict:
        """{'capability_routing.enabled': True} -> {'capability_routing':
        {'enabled': True}} (merge server-side sul blocco esistente)."""
        parts = dotted.split(".")
        root = parts[0]
        if len(parts) == 1:
            return {root: value}
        return {root: {".".join(parts[1:]): value}}

    def _add_row(self, section: str, label: str, value: str, meta: tuple):
        t = self.query_one("#pol-table", DataTable)
        key = f"{section}:{label}:{len(self._row_meta)}"
        self._row_meta[key] = meta
        t.add_row(section, label, value, key=key)

    def _render_table(self) -> None:
        t = self.query_one("#pol-table", DataTable)
        eff = self.effective
        self._row_meta.clear()
        t.clear()
        for _sec, key, label, typ in SCALAR_ROWS:
            self._add_row(_sec, label, self._fmt(self._eff_get(key)),
                          ("scalar", key, typ))
        for _sec, key, label in LIST_ROWS:
            self._add_row(_sec, label, self._fmt(self._eff_get(key)),
                          ("list", key))
        for _sec, key, label in MAP_ROWS:
            m = self._eff_get(key) or {}
            n = len(m)
            sample = next(iter(m), "")
            val = (f"{n} pattern" if n else "(vuota)") + \
                  f"  [dim]es. {sample}[/]" if n else "(vuota)"
            self._add_row(_sec, label, val, ("map", key))
        per_profile = dict(eff.get("profile_step_up_pct") or {})
        profiles_attr = getattr(self.app, "profiles", None)
        known_names = set(per_profile)
        if isinstance(profiles_attr, (list, tuple, set)):
            known_names |= {str(p) for p in profiles_attr}
        for pname in sorted(known_names):
            val = per_profile.get(pname, eff.get("step_up_pct"))
            self._add_row("Salita per profilo", pname,
                          f"{val}%  [dim](globale se assente)[/]",
                          ("profile_step_up", pname))
        # chiavi CLIENT custom per profilo (mascherate; vuoto+invio = rimuovi)
        ckm = eff.get("client_keys_masked") or {}
        for pname in sorted(set(ckm) | set(getattr(self.app, "profiles", None)
                                           or [])):
            val = ckm.get(pname) or "[dim]deterministica sk-<profilo>[/]"
            self._add_row("Chiavi client", pname, f"{pname}: {val}",
                          ("client_key", pname))
        alias_keys_masked = eff.get("alias_keys_masked") or {}
        for k, v in sorted((eff.get("aliases") or {}).items()):
            tag = (f"  [yellow]🔑 {alias_keys_masked[k]}[/]"
                   if k in alias_keys_masked else "")
            self._add_row("Alias", k, f"{k} → [cyan]{v}[/]{tag}",
                          ("alias", k))

    # ------------------------------------------------------------ azioni
    def _selected(self) -> tuple | None:
        t = self.query_one("#pol-table", DataTable)
        if not t.row_count or t.cursor_row is None:
            return None
        keys = list(self._row_meta)
        if 0 <= t.cursor_row < len(keys):
            return self._row_meta[keys[t.cursor_row]]
        return None

    def _row_label(self, key: str) -> str:
        for _s, kk, lbl, _t in SCALAR_ROWS:
            if kk == key:
                return lbl
        for _s, kk, lbl in LIST_ROWS:
            if kk == key:
                return lbl
        return str(key)

    def action_edit_row(self) -> None:
        self.run_worker(self._edit_row_flow(), exclusive=True)

    async def _edit_row_flow(self) -> None:
        meta = self._selected()
        if not meta:
            return
        kind, key = meta

        if kind == "client_key":
            masked = (self.effective.get("client_keys_masked") or {}).get(key)
            hint = f"attuale: {masked}" if masked else \
                "deterministica attiva (nessun override)"
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]Chiave client profilo '{key}'[/b]\n"
                f"[dim]{hint} · min 8 caratteri · vuoto+invio = rimuovi "
                "(riattiva la deterministica sk-...)[/]"))
            if new is None:
                return
            val = new.strip()
            if val and len(val) < 8:
                self.app.notify("la chiave deve avere almeno 8 caratteri",
                                severity="error")
                return
            await self._patch({"client_keys": {key: val}})
            return

        if kind == "alias":
            current = (self.effective.get("aliases") or {}).get(key, "")
            prefix = str(self.effective.get("proxy_prefix") or "")
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]Alias {key}[/b]\ndestinazione col prefisso "
                f"(es. {prefix}<profilo>)", default=str(current)))
            if not new:
                return
            full = dict(self.effective.get("aliases") or {})
            full[str(key)] = new.strip()
            await self._patch({"aliases": full})
            return

        if kind == "profile_step_up":
            pname = key
            current = (self.effective.get("profile_step_up_pct") or {}).get(
                pname, self.effective.get("step_up_pct"))
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]Soglia SALITA profilo '{pname}'[/b]\n%"
                " (100 = solo se non entra; 50 = sale oltre metà contesto)",
                default=str(current)))
            if not new:
                return
            try:
                val = int(float(new))
                assert 1 <= val <= 200
            except (ValueError, AssertionError):
                self.app.notify("la soglia deve essere un numero 1..200",
                                severity="error")
                return
            await self._patch({"profiles": {pname: {"step_up_pct": val}}})
            return

        # scalar / list / map — dispatch sul TIPO dichiarato
        label = self._row_label(key)
        typ = meta[2] if kind == "scalar" and len(meta) > 2 else kind
        current_raw = self._eff_get(key)

        if typ == "bool":
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]{label}[/b]\n[dim]true / false[/]",
                default="true" if current_raw else "false"))
            if new is None:
                return
            low = new.strip().lower()
            if low in ("true", "1", "sì", "si", "yes", "on"):
                await self._patch(self._nested_patch(key, True))
            elif low in ("false", "0", "no", "off"):
                await self._patch(self._nested_patch(key, False))
            else:
                self.app.notify("valori ammessi: true / false",
                                severity="error")
            return

        if typ == "cap_mode":
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]{label}[/b]\n[dim]off = niente strike · suggest = solo "
                "journal · auto = rimozione automatica tracciata[/]",
                default=str(current_raw or "auto")))
            if new is None:
                return
            val = new.strip().lower()
            if val not in ("off", "suggest", "auto"):
                self.app.notify("valori ammessi: off / suggest / auto",
                                severity="error")
                return
            await self._patch(self._nested_patch(key, val))
            return

        if typ == "cap_missing":
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]{label}[/b]\n[dim]dynamic = filtro legacy se la cap non "
                "ha gruppo · error = 400 rigoroso[/]",
                default=str(current_raw or "dynamic")))
            if new is None:
                return
            val = new.strip().lower()
            if val not in ("dynamic", "error"):
                self.app.notify("valori ammessi: dynamic / error",
                                severity="error")
                return
            await self._patch(self._nested_patch(key, val))
            return

        if typ == "map":
            cur_map: dict = dict(current_raw or {})
            default = "; ".join(f"{k}: {','.join(v)}"
                                for k, v in sorted(cur_map.items()))
            new = await self.app.push_screen_wait(TextInputModal(
                f"[b]{label}[/b]\n[dim]una entry per 'pattern: cap,cap' · "
                "più entry separate da ; · caps: text vision video audio "
                "image_gen tools tts stt · vuoto+invio = mappa vuota[/]",
                default=default))
            if new is None:
                return
            parsed: dict[str, list[str]] = {}
            try:
                for chunk in new.split(";"):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    pat, _, caps_part = chunk.partition(":")
                    pat = pat.strip()
                    caps_list = [c.strip() for c in caps_part.split(",")
                                 if c.strip()]
                    if not pat or not caps_list:
                        raise ValueError(chunk)
                    parsed[pat] = caps_list
            except ValueError:
                self.app.notify("formato: pattern: cap,cap ; pattern2: cap",
                                severity="error")
                return
            await self._patch(self._nested_patch(key, parsed))
            return

        sep = ", " if typ == "list" else ""
        default = "" if current_raw is None else \
            (sep.join(str(x) for x in current_raw) if typ == "list"
             else str(current_raw))
        hint = ("lista separata da virgole" if key != "hotwords"
                else "regex separate da |")
        new = await self.app.push_screen_wait(TextInputModal(
            f"[b]{label}[/b]\n[dim]{hint}[/]", default=default))
        if new is None or new.strip() == default.strip():
            return
        if typ == "list":
            split_on = "|" if key == "hotwords" else ","
            items = [x.strip() for x in new.split(split_on) if x.strip()]
            await self._patch(self._nested_patch(key, items))
            return
        if typ == "int":
            try:
                val = int(float(new))
            except ValueError:
                self.app.notify(f"'{key}' deve essere un numero",
                                severity="error")
                return
            await self._patch(self._nested_patch(key, val))
            return
        if typ == "num":
            try:
                val = float(new)
                assert val > 0
            except (ValueError, AssertionError):
                self.app.notify(f"'{key}' deve essere un numero > 0",
                                severity="error")
                return
            await self._patch(self._nested_patch(key, val))
            return
        if not new.strip():
            self.app.notify(f"'{key}' non può essere vuoto",
                            severity="error")
            return
        await self._patch({key: new.strip()})

    def action_add_alias(self) -> None:
        self.run_worker(self._add_alias_flow(), exclusive=True)

    async def _add_alias_flow(self) -> None:
        prefix = str(self.effective.get("proxy_prefix") or "")
        name = await self.app.push_screen_wait(TextInputModal(
            "[b]Nuovo alias[/b]\nnome pubblico richiesto dai client",
            placeholder="es. mio-modello"))
        if not name:
            return
        target = await self.app.push_screen_wait(TextInputModal(
            f"[b]{name}[/b] → destinazione\n[dim]usa il prefisso: "
            f"{prefix}&lt;profilo&gt; o un gruppo {prefix}&lt;profilo&gt;-200k[/]",
            default=f"{prefix}{(self.app.profiles or [''])[0]}"))
        if not target:
            return
        full = dict(self.effective.get("aliases") or {})
        full[name] = target
        await self._patch({"aliases": full})

    def action_del_alias(self) -> None:
        self.run_worker(self._del_alias_flow(), exclusive=True)

    async def _del_alias_flow(self) -> None:
        meta = self._selected()
        if not meta or meta[0] != "alias":
            self.app.notify("seleziona prima una riga ALIAS",
                            severity="warning")
            return
        name = meta[1]
        full = {k: v for k, v in (self.effective.get("aliases") or {}).items()
                if k != name}
        # rimuovendo l'alias, rimuove anche l'eventuale chiave custom
        # associata (altrimenti resterebbe orfana e verrebbe rifiutata
        # dalla validazione al prossimo caricamento)
        await self._patch({"aliases": full, "alias_keys": {name: ""}})

    def action_alias_key(self) -> None:
        self.run_worker(self._alias_key_flow(), exclusive=True)

    async def _alias_key_flow(self) -> None:
        meta = self._selected()
        if not meta or meta[0] != "alias":
            self.app.notify("seleziona prima una riga ALIAS",
                            severity="warning")
            return
        name = meta[1]
        target = (self.effective.get("aliases") or {}).get(name, "")
        masked = (self.effective.get("alias_keys_masked") or {}).get(name)
        hint = f"attuale: {masked}" if masked else \
            "nessuna chiave impostata"
        new = await self.app.push_screen_wait(TextInputModal(
            f"[b]Chiave custom per alias '{name}'[/b] → {target}\n"
            f"[dim]{hint} · min 8 caratteri · vuoto+invio = rimuovi "
            "(torna al pool del profilo) · effettiva SOLO se l'alias è "
            "generico (nessun suffisso -Nk/-go/-fallback)[/]"))
        if new is None:
            return                      # esc: annulla senza modifiche
        # NOTA: qui il submit VUOTO è un'azione valida (= cancella la chiave),
        # a differenza degli altri flussi dove vuoto = annulla.
        await self._patch({"alias_keys": {name: new.strip()}})

    async def _patch(self, patch: dict) -> None:
        try:
            await self.client.policy_patch(patch)
        except GatewayError as exc:
            self.app.notify(f"PATCH rifiutata: {exc.message}",
                            severity="error")
            return
        self.app.notify(f"policy applicata: {list(patch)}")
        await self.reload()
