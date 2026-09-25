"""STT: rimozione delle allucinazioni di credit di sottotitolaggio.

Whisper (large-v3/turbo) su silenzio/rumore inventa credit tipo
"Sottotitoli e revisione a cura di QTSS" (a volte riecheggiati dal prompt in
forma parziale, es. "e revisione a cura di QTSS"). `app.sttscrub` li rimuove
dalla risposta STT, qualunque sia il backend. Qui si blinda l'alta precisione:
via le varianti note, intatta la dettatura reale.
"""
from __future__ import annotations

from app.sttscrub import scrub_payload, scrub_text

_VARIANTS = [
    "Sottotitoli e revisione a cura di QTSS.",
    "Sottotitoli e revisione a cura di QTSS",
    "Sottotitoli a cura di QTSS",
    "Sottotitoli a cura di Whisper",
    "Sottotitoli a cura di",
    # output parziale / riecheggiato dal prompt di contesto:
    "e revisione a cura di QTSS",
    "revisione a cura di QTSS.",
    # frammento senza intestazione:
    "a cura di QTSS",
    "Sottotitoli e revisione a cura di amara.org",
]


def test_scrub_varianti_note():
    for v in _VARIANTS:
        out, n = scrub_text(v)
        assert n >= 1, v
        assert out == "", (v, out)


def test_scrub_credit_incastonato_in_testo_reale():
    out, n = scrub_text(
        "Ciao, questa e' una dettatura reale. "
        "Sottotitoli e revisione a cura di QTSS.")
    assert n == 1
    assert out == "Ciao, questa e' una dettatura reale."


def test_scrub_non_tocca_dettatura_reale():
    for ok in ("Ciao, come stai?",
               "Grazie a tutti, ci vediamo domani.",
               "il testo e' a cura di Marco",
               "la revisione a cura di Luca e' pronta",
               "Sottotitoli in inglese per il film"):
        out, n = scrub_text(ok)
        assert n == 0, ok
        assert out == ok, ok


def test_scrub_stringa_vuota():
    assert scrub_text("") == ("", 0)
    assert scrub_text(None) == (None, 0)


def test_scrub_payload_dict_con_segments():
    payload = {
        "text": "Ciao mondo. Sottotitoli e revisione a cura di QTSS",
        "segments": [
            {"text": "Ciao mondo."},
            {"text": "e revisione a cura di QTSS"},
        ],
    }
    out, n = scrub_payload(payload)
    assert n == 2
    assert out["text"] == "Ciao mondo."
    assert out["segments"][0]["text"] == "Ciao mondo."
    assert out["segments"][1]["text"] == ""


def test_scrub_payload_formato_srt_vtt():
    srt = ("1\n00:00:00,000 --> 00:00:02,000\n"
           "Ciao mondo.\n\n"
           "2\n00:00:02,000 --> 00:00:04,000\n"
           "Sottotitoli e revisione a cura di QTSS\n")
    out, n = scrub_payload(srt)
    assert n == 1
    assert "QTSS" not in out
    assert "Ciao mondo." in out


def test_scrub_payload_tipo_non_testuale():
    out, n = scrub_payload(123)
    assert n == 0 and out == 123
