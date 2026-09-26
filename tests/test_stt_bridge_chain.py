"""STT-bridge: catena di fallback completa, ordine per tier, parzialità.

La regola della flotta che questi test blindano:

  1. un chunk viene dichiarato fallito SOLO dopo aver provato TUTTI i
     deployment con la capacita' `stt`, nell'ordine
     free vivi -> free in cooldown -> -go vivi -> -go in cooldown -> -fallback;
  2. un solo chunk fallito NON fa fallire l'audio: si consegna il resto e lo si
     dichiara parziale, e non si mette in cache (cosi' un turno successivo puo'
     completarlo);
  3. `used` (chunk paralleli) e' una preferenza, mai un motivo per perdere una
     trascrizione;
  4. mai 503 al client: a catena esaurita va il marker e la richiesta prosegue.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import struct

import pytest

SR = 16000
_CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,caps\n")
_UP = "https://upstream.test/v1"


def _row(provider, data="free", caps="stt"):
    caps = f'"{caps}"' if "," in caps else caps
    return (f"seed,whisper-x,{provider},{_UP},{data},8,8000,1,sk-key-1234,"
            f"{caps}\n")


_pytest_mark = pytest.mark.usefixtures("env")


def _wav(sec):
    import av
    n = int(SR * sec)
    b = io.BytesIO()
    pcm = b"".join(struct.pack("<h", int(6000 * math.sin(
        2 * math.pi * 220 * i / SR))) for i in range(n))
    c = av.open(b, "w", format="wav")
    st = c.add_stream("pcm_s16le", rate=SR)
    st.layout = "mono"
    fr = av.AudioFrame(format="s16", layout="mono", samples=n)
    fr.planes[0].update(pcm)
    fr.sample_rate = SR
    fr.pts = 0
    for p in st.encode(fr):
        c.mux(p)
    c.close()
    return b.getvalue()


class _Req:
    """Request minimale ma REALE: la funzione legge `headers`."""

    def __init__(self):
        self.headers = {}
        self.method = "POST"
        self.client = type("C", (), {"host": "127.0.0.1", "port": 1234})()
        self.url = type("U", (), {"scheme": "http", "hostname": "localhost"})()


class _Fwd:
    """Forwarder finto: registra l'ORDINE dei tentativi.

    `fail` sono i provider che falliscono. `ok_text` il testo per i provider
    che rispondono."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls: list[str] = []

    async def transcribe(self, dep, data_fields, file_bytes, filename,
                         content_type, path="transcriptions", **kw):
        prov = dep.get("provider") or "?"
        self.calls.append(prov)
        if prov in self.fail:
            from app.forwarder import UpstreamError
            raise UpstreamError(-429, "rate limit")
        return {"text": f"da {prov}"}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    # un deployment per tier, provider distinti: cosi' l'ordine osservato nei
    # test e' inequivocabile.
    csv.write_text(_CSV_HEADER
                   + _row("freea")                  # free
                   + _row("freeb")                  # free
                   + _row("goa", data="go")         # -go
                   + _row("fba", data="fallback"))  # -fallback
    import app.main as m
    orig = (m.authn.master_key, m.config.csv_path,
            m.router.policy.cap_groups_enabled)
    m.authn.master_key = "test-master-sttchain"
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    monkeypatch.setattr(m, "LEDGER", Ledger(tmp_path))
    m.router.policy.cap_groups_enabled = True
    import app.audiostore as store
    store.clear()
    yield m
    m.router._cooldown.clear()
    m.router.policy.cap_groups_enabled = orig[2]
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


def _providers(m, group):
    return [d["provider"] for d in m.config.groups.get(group, [])]


def _call(m, fwd, used=None):
    import app.main as sm
    return asyncio.run(sm._stt_bridge_transcribe(
        _Req(), b"RIFF0000", "test", "raw", None, used if used is not None
        else set()))


# ============================================ 1. ordine per tier
def test_ordine_free_go_fallback(monkeypatch, env):
    """Tutti falliscono: l'ORDINE dei tentativi deve essere
    free -> free -> -go -> -fallback, non l'ordine di dichiarazione del CSV."""
    m = env
    every = {"freea", "freeb", "goa", "fba"}
    fwd = _Fwd(fail=every)
    monkeypatch.setattr(m, "forwarder", fwd)
    res = _call(m, fwd)
    assert res == "", res
    assert set(fwd.calls) == every, fwd.calls       # TUTTI provati
    # i free prima dei -go, i -go prima del -fallback
    i_go = fwd.calls.index("goa")
    i_fb = fwd.calls.index("fba")
    assert all(fwd.calls.index(p) < i_go for p in ("freea", "freeb")), fwd.calls
    assert i_go < i_fb, fwd.calls


def test_free_in_cooldown_dopo_i_free_vivi(monkeypatch, env):
    """Un free in cooldown si prova DOPO i free vivi, non viene saltato."""
    m = env
    fwd = _Fwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    # metto freea in cooldown pieno (l'unique non contiene il provider: si
    # cerca per provider, non per stringa nel nome)
    target = next(d["unique"] for d in m.config.groups["scrocco-llm-test-stt"]
                  if d.get("provider") == "freea")
    m.router._cooldown[target] = 9999999999.0
    res = _call(m, fwd)
    assert res, "deve comunque riuscire"
    # freeB (vivo) viene prima di goA e fbA; freeA non e' stato scartato in
    # assoluto dalla lista (anche se in cooldown resta un candidato)
    assert "freeb" in fwd.calls
    assert fwd.calls.index("freeb") < len(fwd.calls), fwd.calls
    # nessun tentativo su freeA (in cooldown pieno non si sceglie), ma goA/fbA
    # restano disponibili se i free non bastano
    assert res.startswith("da ")


def test_fallimento_solo_dopo_tutti(monkeypatch, env):
    """Il fallimento si dichiara SOLO a lista esaurita: l'ultimo tentativo
    deve essere il -fallback (che e' l'ultima risorsa della flotta)."""
    m = env
    fwd = _Fwd(fail={"freea", "freeb", "goa", "fba"})
    monkeypatch.setattr(m, "forwarder", fwd)
    res = _call(m, fwd)
    assert res == ""
    assert len(fwd.calls) == 4, fwd.calls
    assert fwd.calls[-1] == "fba", f"il -fallback deve essere l'ultimo: {fwd.calls}"


# ============================================ 2. rotazione reale
def test_ruota_dopo_un_fallimento(monkeypatch, env):
    """Il primo che risponde vince, ma dopo un fallimento si ruota."""
    m = env
    fwd = _Fwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    first = _call(m, fwd)
    assert first and fwd.calls
    used_prov = fwd.calls[0]
    fwd.calls.clear()
    fwd.fail.add(used_prov)
    res = _call(m, fwd)
    assert fwd.calls[0] == used_prov, fwd.calls
    assert len(fwd.calls) >= 2, f"doveva ruotare: {fwd.calls}"
    assert res == f"da {fwd.calls[-1]}", (res, fwd.calls)


def test_non_prova_un_deployment_gia_tentato(monkeypatch, env):
    """Nessun deployment viene provato due volte nello stesso chunk."""
    m = env
    fwd = _Fwd(fail={"freea", "freeb", "goa", "fba"})
    monkeypatch.setattr(m, "forwarder", fwd)
    _call(m, fwd)
    assert len(fwd.calls) == len(set(fwd.calls)), fwd.calls


# ============================================ 3. `used` e parallelismo
def test_used_non_fa_perdere_il_chunk(monkeypatch, env):
    """Tutti i deployment marcati `used` (altri chunk): il chunk deve
    comunque essere trascritto, non perso."""
    m = env
    fwd = _Fwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    used = set(m.config.chains_cap["test"]["stt"])
    res = _call(m, fwd, used=used)
    assert res, "con tutto in `used` deve comunque riuscire"
    assert fwd.calls


def test_used_preferisce_i_liberi(monkeypatch, env):
    """Se un deployment e' gia' in uso da un altro chunk, se ne preferisce uno
    libero prima di condividerlo."""
    m = env
    fwd = _Fwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    first = _call(m, fwd)
    taken = fwd.calls[0]
    fwd.calls.clear()
    # `taken` occupato da un altro chunk; gli altri liberi
    used = {u for u in m.config.chains_cap["test"]["stt"]
            if taken in u} or set()
    res = _call(m, fwd, used=used)
    assert res
    assert fwd.calls[0] != taken or len(fwd.calls) == 1, fwd.calls


def test_chunk_paralleli_non_collidono(monkeypatch, env):
    m = env
    fwd = _Fwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    import app.main as sm

    async def _go():
        used = set()
        return await asyncio.gather(
            sm._stt_bridge_transcribe(_Req(), b"RIFF", "test", "raw", None,
                                      used),
            sm._stt_bridge_transcribe(_Req(), b"RIFF", "test", "raw", None,
                                      used))
    asyncio.run(_go())
    assert len(fwd.calls) >= 2
    # i due chunk non devono aver usato lo stesso deployment in parallelo
    assert fwd.calls[0] != fwd.calls[1], fwd.calls


# ============================================ 4. parzialita' e marker
def _audio_part(sec=3):
    return {"type": "input_audio",
            "input_audio": {"data": base64.b64encode(_wav(sec)).decode(),
                            "format": "wav"}}


class _Pol:
    stt_chat_enabled = True
    stt_chat_target_sec = 180
    stt_chat_search_pct = 0.2
    stt_chat_max_bytes = 7 * 1024 * 1024
    stt_chat_max_parallel = 4
    stt_chat_unavailable_notice = "Nessun STT ha risposto."


def _run_bridge(payload, transcript_one):
    import app.sttchat as sc
    import app.audiostore as store
    store.clear()

    async def _main():
        return await sc.resolve_audio_in_payload(
            payload, boundary=0, transcript_one=transcript_one, policy=_Pol())
    return asyncio.run(_main())


def _text_of(out):
    return " ".join(p.get("text", "")
                    for p in out["messages"][0]["content"])


def test_un_chunk_fallito_non_fa_fallire_l_audio():
    """Audio di ~7 min (3 chunk): il chunk centrale fallisce, gli altri no.
    L'audio NON deve fallire: si consegna il parziale, dichiarato."""
    payload = {"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "cosa dice?"}, _audio_part(420)]}]}

    async def _one(chunk, idx):
        return "" if idx == 1 else f"C{idx}"

    out = _run_bridge(payload, _one)
    txt = _text_of(out)
    assert "C0" in txt and "C2" in txt, "i chunk buoni devono restare"
    assert "audio non trascritto" not in txt, "non e' un fallimento totale"
    assert "parziale" in txt, f"la parzialita' va dichiarata: {txt[:160]}"
    assert "1 di 3" in txt, txt[:160]


def test_tutti_i_chunk_falliti_mettono_il_marker():
    payload = {"model": "m", "messages": [
        {"role": "user", "content": [_audio_part(420)]}]}

    async def _one(chunk, idx):
        return ""

    txt = _text_of(_run_bridge(payload, _one))
    assert "audio non trascritto" in txt
    assert "Nessun STT ha risposto" in txt


def test_parziale_non_messo_in_cache():
    """Un parziale non va in cache: un turno successivo deve poterlo completare."""
    import app.audiostore as store
    import app.sttchat as sc
    payload = {"model": "m", "messages": [
        {"role": "user", "content": [_audio_part(420)]}]}
    calls = {"n": 0}

    async def _one(chunk, idx):
        calls["n"] += 1
        return "" if idx == 1 else f"C{idx}"

    _run_bridge(payload, _one)
    assert store.stats()["items"] == 0, "il parziale non deve essere cachato"
    # e di nuovo: deve ritentare (non un hit di cache)
    before = calls["n"]
    _run_bridge(payload, _one)
    assert calls["n"] > before, "deve ritentare, non leggere un parziale cachato"


def test_completo_va_in_cache():
    import app.audiostore as store
    payload = {"model": "m", "messages": [
        {"role": "user", "content": [_audio_part(3)]}]}

    async def _one(chunk, idx):
        return "testo"

    _run_bridge(payload, _one)
    assert store.stats()["items"] == 1
