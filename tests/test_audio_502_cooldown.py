"""REGOLA: un 502 va SEMPRE in cooldown, qualunque cosa chieda il client.

Un errore di TRASPORTO verso il deployment (timeout, connessione rifiutata,
DNS non risolto — il caso reale: container locale SPENTO) arriva al gateway
come `UpstreamError(status=None)`. Deve essere trattato come errore DEL
DEPLOYMENT: `mark_failed` (cooldown) e, con una chiave di profilo, rotazione
sul successivo (`fallback_next` e' saltato quando il profilo e' vuoto, come
per la master key).

Regressione: i predicati `deployment_side` di TTS e STT usavano `status > 0`,
cosi' lo status 0/None cadeva nel ramo `client_error`, che rispondeva 502 al
client SENZA cooldown. In produzione: la dictation falliva a intermittenza
con 502 da ~200 ms su `http://speaches-gpu:8000/v1` (container spento) e il
deployment morto non entrava MAI in cooldown — zero voci STT fra i cooldown
attivi. Il path `systemone` aveva gia' `status >= 0`: ora sono allineati.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.forwarder import UpstreamError
import app.state as gw_state

_CSV_HEADER = "commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps\n"
# Container locale spento: l'endpoint non risolve.
_ROW_MORTO = "a@x.com,whisper-gpu,speaches,http://speaches-gpu:8000/v1,free,8,8000,1,sk-stt-a,stt\n"
_ROW_MORTO_2 = "b@x.com,whisper-cpu,speaches,http://speaches:8000/v1,free,8,8000,2,sk-stt-b,stt\n"

MK = {"Authorization": "Bearer test-master-stt"}


class _FakeFwd:
    """Ogni chiamata fallisce per TRASPORTO (status None, non un HTTP 4xx)."""

    def __init__(self, ok_after: int = 0):
        self.calls: list[str] = []
        self.ok_after = ok_after

    async def transcribe(self, dep, data_fields, file_bytes, filename, content_type, path="transcriptions", **kw):
        self.calls.append(dep["unique"])
        if len(self.calls) > self.ok_after:
            raise UpstreamError(None, "upstream connection error: gaierror -2 Name or service not known")
        return {"text": "ciao", "nx_deployment": dep["unique"]}


@pytest.fixture()
def make_client(monkeypatch, tmp_path):
    def _build(*rows):
        csv = tmp_path / "k.csv"
        csv.write_text(_CSV_HEADER + "".join(rows))
        import app.main as m

        gw_state.authn.master_key = "test-master-stt"
        gw_state.LEDGER.flush()
        monkeypatch.setattr(gw_state, "VAR_DIR", str(tmp_path))
        monkeypatch.setattr(gw_state, "CSV_PATH", str(csv))
        monkeypatch.setattr(gw_state.config, "csv_path", csv)
        gw_state.config.reload()
        gw_state.router.policy.cap_groups_enabled = True
        gw_state.router._cooldown.clear()
        return TestClient(m.app)

    yield _build
    gw_state.router._cooldown.clear()
    gw_state.router.policy.cap_groups_enabled = False
    gw_state.config.reload()


def _post(c, monkeypatch, fake):
    monkeypatch.setattr(gw_state, "forwarder", fake)
    return c.post(
        "/v1/audio/transcriptions",
        headers=MK,
        data={"model": "scrocco-llm-test"},
        files={"file": ("a.wav", b"RIFF0000", "audio/wav")},
    )


def test_trasporto_stt_mette_in_cooldown_il_deployment(make_client, monkeypatch):
    """Il cuore della regola: se il client riceve un 502, il deployment che
    l'ha causato e' in cooldown (prima del fix NON lo era)."""
    c = make_client(_ROW_MORTO)
    fake = _FakeFwd()
    r = _post(c, monkeypatch, fake)
    assert r.status_code == 502, r.text
    assert fake.calls, "nessun tentativo effettuato"
    assert gw_state.router.is_cooled_down(fake.calls[0]) is True


def test_ogni_tentativo_fallito_resta_in_cooldown(make_client, monkeypatch):
    """Con due deployment, nessuno dei tentativi torna disponibile subito."""
    c = make_client(_ROW_MORTO, _ROW_MORTO_2)
    fake = _FakeFwd()
    r = _post(c, monkeypatch, fake)
    assert r.status_code == 502, r.text
    assert all(gw_state.router.is_cooled_down(u) for u in fake.calls), fake.calls


def test_un_4xx_di_merito_non_brucia_il_deployment(make_client, monkeypatch):
    """Distinzione che il fix NON deve rompere: un 4xx negativo (errore della
    richiesta) non e' un errore di trasporto e non entra come tale."""
    c = make_client(_ROW_MORTO)
    fake = _FakeFwd()
    fake.transcribe = _raise_400  # type: ignore[method-assign]
    r = _post(c, monkeypatch, fake)
    assert r.status_code == 400, r.text


async def _raise_400(dep, *a, **kw):
    raise UpstreamError(-400, '{"error":{"message":"payload schema"}}')
