"""Modali riutilizzabili della TUI (conferme, input, info, aiuto)."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "Sì"),
                Binding("n", "no", "No"),
                Binding("escape", "no", "Annulla")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self):
        with Vertical(id="modal-box"):
            yield Label(self.question)
            yield Label("[dim]y = sì · n/esc = annulla[/dim]", id="hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class TypedConfirmModal(ModalScreen[bool]):
    """Conferma distruttiva: va digitata ESATTAMENTE la parola richiesta."""

    BINDINGS = [Binding("escape", "cancel", "Annulla")]

    def __init__(self, question: str, word: str):
        super().__init__()
        self.question = question
        self.word = word

    def compose(self):
        with Vertical(id="modal-box"):
            yield Label(f"{self.question}\n[dim]digita [b]{self.word}[/b] "
                        "per confermare[/]")
            yield Input(placeholder=self.word, id="typed")
            yield Label("[dim]invio = conferma · esc = annulla[/dim]",
                        id="hint")

    def on_mount(self) -> None:
        self.query_one("#typed", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() == self.word)

    def action_cancel(self) -> None:
        self.dismiss(False)


class InfoModal(ModalScreen[None]):
    BINDINGS = [Binding("enter", "close", "Ok"),
                Binding("escape", "close", "Chiudi")]

    def __init__(self, message: str, error: bool = False):
        super().__init__()
        self.message = message
        self.error = error

    def compose(self):
        classes = "modal-box modal-error" if self.error else "modal-box"
        with Vertical(classes=classes.split()):
            yield Static(self.message)

    def action_close(self) -> None:
        self.dismiss(None)


class TextInputModal(ModalScreen[str | None]):
    """Input singolo con titolo; ritorna la stringa o None se annullato."""

    BINDINGS = [Binding("escape", "cancel", "Annulla")]

    def __init__(self, title: str, placeholder: str = "", default: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.default = default

    def compose(self):
        with Vertical(id="modal-box"):
            yield Label(self.title_text)
            yield Input(placeholder=self.placeholder, value=self.default,
                        id="txt")
            yield Label("[dim]invio = conferma · esc = annulla[/dim]",
                        id="hint")

    def on_mount(self) -> None:
        self.query_one("#txt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Chiudi"),
                Binding("question_mark", "close", "Chiudi", key_display="?")]

    HELP = """\
[b cyan]Gateway Installer — scrocco-llm[/]

[b]Globale[/]
  r          ricarica i dati dal gateway
  R          forza reload CSV+policy sul gateway remoto
  C          sblocca TUTTI i cooldown attivi
  S          rilascia TUTTE le sticky session
  Y          pagina AVANZATE: policy completa (prefissi, suffissi,
             soglia salita globale e PER PROFILO, alias, timing, hotword,
             routing per capacità (gruppi -vision ecc.), auto-learn,
             escalation cooldown, QC sanity)
  M          pagina CAPACITÀ: per_capability, gruppi primary/go/fallback,
             catene fallback, strike auto-learn, contatori tts/stt, health
  K          chiavi client (sk-<profilo>) e master mascherata
  E          report rotazioni in arrivo entro N giorni
  O          osservabilità: chiamate live, errori tracciati, classifica deployment
  ?          questo aiuto      ·   q esci

[b]Tabella deployment[/]
  /          filtra modello/provider/endpoint/gruppo/caps (invio applica)
  n          nuovo deployment     ·   INVIO o e : modifica selezionato
  d          elimina deployment   ·   p  nuovo profilo
  x          elimina profilo (conferma digitando il nome)

[b]Colonna caps[/]
  V vision · D video-ingest · A audio-chat · I image_gen · G video_gen
  T tools · S stt · P tts — "text" = solo testo censito
  I/G vivono SOLO nei gruppi generazione (-image_gen/-video_gen): mai
  nelle catene di analisi, mai come fallback del chat.

[b]Fallback di scopo[/]
  I gruppi capacità sono strutture REALI: -vision/-video/-audio/
  -image_gen/-tts/-stt, ognuna con la STESSA terna primary/-go/-fallback
  del mondo testo.
  Richieste BASE (routing automatico): il dispatcher sceglie il gruppo
  capacità giusto (mai un text-only per una vision, ecc.).
  Richieste ESPLICITE (-vision, -go/-fallback/univoco del gruppo):
  pass-through; i retry ruotano SOLO dentro quel gruppo, senza sconfinare.
  Guardia SOFT sul max_input applicata DENTRO ogni capacità; membership
  decisa dalla colonna caps del CSV (token 'text' = anche nei dims).
  Lo schermo M mostra gruppi e catene di fallback per ogni capacità.

[b]Pagina AVANZATE (Y)[/]
  e          modifica il valore della riga selezionata
  a          aggiunge un alias (nome -> destinazione col prefisso)
  D          elimina l'alias selezionato
  k          imposta/rimuove la chiave CUSTOM dell'alias selezionato
             (solo alias generici; vuoto+invio = rimuove, torna al pool)
  invio/esc  chiude (le modifiche sono già applicate)

Ogni operazione è ATOMICA e IMMEDIATE via API: nessun bottone "applica".
"""

    def compose(self):
        with Vertical(id="help-box"):
            yield Static(self.HELP)

    def action_close(self) -> None:
        self.dismiss(None)
