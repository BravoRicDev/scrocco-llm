"""Vision/media nella chat: riconoscimento delle forme, rifiuto esplicito,
tetto alle immagini inviate (intervento #57, Fase 1/2/3).

Cosa viene blindato qui:
  Fase 1 - `required_caps` e `count_image_parts` riconoscono le forme
           Gemini-native (`inline_data`, `file` con mime image/*) oltre allo
           standard OpenAI: senza, un'immagine in quelle forme finiva nel
           mondo TESTO e il modello non la vedeva; inoltre la stima token
           sbagliava (rischio ctx-overflow surprising);
  Fase 2 - un deployment ESPLICITO (unique) che non dichiara la capacita'
           media richiesta produce 400, non un passthrough: un text-only che
           riceve un'immagine risponderebbe inventandola. Dimensioni, base
           generico e alias continuano a scalare (comportamento camaleontico);
  Fase 3 - `capability_routing.chat_images_max` limita quante immagini della
           history vengono inviate all'upstream, con priorita' al turno
           corrente, senza mutare la history del client e senza toccare la
           stima del contesto.
"""
import base64

import pytest
from fastapi.testclient import TestClient

from app.capabilities import (count_image_parts, required_caps,
                              wants_image_output)
from app.main import _trim_chat_images

_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_URI = "data:image/png;base64," + _PNG


def _u(payload):
    return required_caps(payload)


# ============================================================ Fase 1: forme
def test_f1_openai_image_url_riconosciuta():
    p = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _URI}}]}]}
    assert _u(p) == frozenset({"vision"})
    assert count_image_parts(p["messages"]) == 1


def test_f1_input_image_gemini_riconosciuta():
    p = {"messages": [{"role": "user", "content": [
        {"type": "input_image", "image_url": _URI}]}]}
    assert _u(p) == frozenset({"vision"})
    assert count_image_parts(p["messages"]) == 1


def test_f1_inline_data_immagine_ora_riconosciuta():
    """Prima della Fase 1 questa forma NON aggiungeva vision: la richiesta
    finiva nel mondo testo e il modello non vedeva l'immagine."""
    p = {"messages": [{"role": "user", "content": [
        {"type": "inline_data", "mime_type": "image/png", "data": _PNG}]}]}
    assert _u(p) == frozenset({"vision"})
    assert count_image_parts(p["messages"]) == 1


def test_f1_file_immagine_ora_riconosciuta():
    p = {"messages": [{"role": "user", "content": [
        {"type": "file", "file": {"mime_type": "image/jpeg",
                                  "file_data": _PNG}}]}]}
    assert _u(p) == frozenset({"vision"})
    assert count_image_parts(p["messages"]) == 1


def test_f1_video_non_regresso():
    """`inline_data`/`file` con mime video restano video, non immagini."""
    p1 = {"messages": [{"role": "user", "content": [
        {"type": "inline_data", "mime_type": "video/mp4", "data": "AA"}]}]}
    p2 = {"messages": [{"role": "user", "content": [
        {"type": "file", "file": {"mime_type": "video/webm"}}]}]}
    assert _u(p1) == frozenset({"video"})
    assert _u(p2) == frozenset({"video"})
    # un video NON e' un'immagine: non deve finire nel conteggio
    assert count_image_parts(p1["messages"]) == 0
    assert count_image_parts(p2["messages"]) == 0


def test_f1_audio_e_testo_non_toccat_i():
    assert _u({"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {}}]}]}) == frozenset({"audio"})
    assert _u({"messages": [{"role": "user", "content": "ciao"}]}) \
        == frozenset()
    assert _u({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "ciao"}]}]}) == frozenset()


def test_f1_routing_e_stima_vedono_la_stessa_immagine():
    """required_caps e count_image_parts non possono discordare: se il routing
    dice vision, anche la stima dei token deve contare quell'immagine."""
    forms = [
        {"type": "image_url", "image_url": {"url": _URI}},
        {"type": "input_image", "image_url": _URI},
        {"type": "inline_data", "mime_type": "image/png", "data": _PNG},
        {"type": "file", "file": {"mime_type": "image/png", "file_data": _PNG}},
    ]
    for part in forms:
        p = {"messages": [{"role": "user", "content": [part]}]}
        assert "vision" in _u(p), part
        assert count_image_parts(p["messages"]) == 1, part


def test_f1_mix_video_e_immagine_contati_giusti():
    p = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _URI}},
        {"type": "inline_data", "mime_type": "video/mp4", "data": "AA"},
        {"type": "inline_data", "mime_type": "image/png", "data": _PNG}]}]}
    assert _u(p) == frozenset({"vision", "video"})
    assert count_image_parts(p["messages"]) == 2


# ================================================= Fase 2: rifiuto esplicito
_CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,image_via,caps\n")
_UP = "https://upstream.test/v1"


def _row(caps, model="some-text-model", provider="groq", via="both"):
    caps = f'"{caps}"' if "," in caps else caps
    return (f"seed,{model},{provider},{_UP},free,8,8000,1,sk-key-1234,"
            f"{via},{caps}\n")


def _make_client(monkeypatch, tmp_path, csv_text):
    csv = tmp_path / "k.csv"
    csv.write_text(csv_text)
    import app.main as m
    orig = (m.authn.master_key, m.config.csv_path,
            m.router.policy.cap_groups_enabled)
    m.authn.master_key = "test-master-vision"
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    monkeypatch.setattr(m, "LEDGER", Ledger(tmp_path))
    m.router.policy.cap_groups_enabled = True
    return TestClient(m.app), m, orig


def _teardown(m, orig):
    m.router.policy.cap_groups_enabled = orig[2]
    m.router._cooldown.clear()
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


MK = {"Authorization": "Bearer test-master-vision"}

# un deployment text-only e uno vision
_CSV_MIX = (_CSV_HEADER
            + _row("text")
            + _row("vision", model="vision-model", provider="openai"))


def _unique(c, provider):
    """Nome unique del deployment del provider indicato."""
    import app.main as m
    for g, deps in m.config.groups.items():
        for d in deps:
            if (d.get("provider") or "") == provider:
                return d["unique"]
    raise AssertionError(provider)


def test_f2_unique_text_only_con_immagine_da_400(monkeypatch, tmp_path):
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    try:
        u = _unique(c, "groq")
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": u, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "cosa c'e' qui?"},
                {"type": "image_url", "image_url": {"url": _URI}}]}]})
        assert r.status_code == 400, r.text
        err = r.json()["error"]
        assert err.get("code") == "model_capability_unsupported"
        assert "vision" in err.get("missing_capabilities", [])
        # il messaggio dice che il MODELO non supporta, non di configurare yaml
        assert "vision" in err["message"]
        assert "gateway.yaml" not in err["message"]
    finally:
        _teardown(m, orig)


class _FakeForwarder:
    """Forwarder finto: risponde a qualunque modello senza rete."""

    def __init__(self):
        self.calls: list[str] = []

    async def call(self, dep, payload, **kw):
        self.calls.append(dep.get("provider") or dep.get("unique", "?"))
        return {"choices": [{"message": {"content": "ok"}}]}

    async def call_images(self, dep, payload, **kw):
        self.calls.append(dep.get("provider") or dep.get("unique", "?"))
        return {"data": [{"b64_json": _PNG}]}

    async def stream_response(self, dep, payload, **kw):
        self.calls.append(dep.get("provider") or dep.get("unique", "?"))

        async def _gen():
            yield (b'data: {"choices":[{"delta":{"content":"ok"},'
                   b'"finish_reason":"stop"}]}\n\n')
            yield b"data: [DONE]\n\n"
        return _gen()


def _patch_forwarder(monkeypatch, m):
    fwd = _FakeForwarder()
    monkeypatch.setattr(m, "forwarder", fwd)
    return fwd


def test_f2_unique_vision_con_immagine_funziona(monkeypatch, tmp_path):
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    _patch_forwarder(monkeypatch, m)
    try:
        u = _unique(c, "openai")
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": u, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "cosa c'e' qui?"},
                {"type": "image_url", "image_url": {"url": _URI}}]}]})
        assert r.status_code == 200, r.text
    finally:
        _teardown(m, orig)


def test_f2_unique_text_only_senza_immagine_va_pure(monkeypatch, tmp_path):
    """Il rifiuto vale per le capacita' MEDIA: una chat di testo normale su un
    unique text-only deve continuare a funzionare."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    _patch_forwarder(monkeypatch, m)
    try:
        u = _unique(c, "groq")
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": u, "messages": [{"role": "user", "content": "ciao"}]})
        assert r.status_code == 200, r.text
    finally:
        _teardown(m, orig)


def test_f2_dimensione_scala_anche_se_text_only(monkeypatch, tmp_path):
    """Una DIMENSIONE non e' un modello esplicito: con un'immagine deve
    salire al gruppo vision, non rispondere 400 (comportamento camaleontico)."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    try:
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": "scrocco-llm-test-8k", "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "cosa c'e' qui?"},
                    {"type": "image_url", "image_url": {"url": _URI}}]}]})
        assert r.status_code != 400, r.text
    finally:
        _teardown(m, orig)


def test_f2_rifiuto_riguarda_anche_image_gen(monkeypatch, tmp_path):
    """La regola vale per QUALSIASI capacita' media, non solo vision."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    try:
        u = _unique(c, "groq")
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": u, "modalities": ["image"],
            "messages": [{"role": "user", "content": "un cubo"}]})
        assert r.status_code == 400, r.text
        assert "image_gen" in r.json()["error"].get(
            "missing_capabilities", [])
    finally:
        _teardown(m, orig)


def test_f2_cooldown_non_produce_400(monkeypatch, tmp_path):
    """Un 400 'non puo' non deve dipendere da un cooldown TEMPORANEO: qui il
    deployment vision e' in cooldown ma la richiesta su un unique vision deve
    comunque non essere rifiutata per capacita'."""
    import app.main as m
    c, m2, orig = _make_client(monkeypatch, tmp_path, _CSV_MIX)
    try:
        u = _unique(c, "openai")
        m2.router._cooldown[u] = 9999999999.0
        r = c.post("/v1/chat/completions", headers=MK, json={
            "model": u, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "cosa c'e'?"},
                {"type": "image_url", "image_url": {"url": _URI}}]}]})
        # non un 400 di CAPACITA': o va (503 se tutto in cooldown) o funziona
        assert r.status_code != 400, r.text
    finally:
        _teardown(m2, orig)


# ============================================ Fase 3: tetto immagini history
def _hist(n_imgs, n_msgs=1):
    msgs = []
    for i in range(n_msgs):
        content = [{"type": "text", "text": f"msg{i}"}]
        for j in range(n_imgs):
            content.append({"type": "image_url",
                            "image_url": {"url": f"m{i}i{j}"}})
        msgs.append({"role": "user", "content": content})
    return msgs


def _kept_urls(out):
    return [p["image_url"]["url"] for m in out["messages"]
            if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"]


def test_f3_tetto_0_disabilita_il_trimming():
    p = {"messages": _hist(5)}
    out, drop = _trim_chat_images(p, 0)
    assert drop == 0
    assert out is p                      # nessuna copia inutile


def test_f3_nessun_trimming_sotto_tetto():
    p = {"messages": _hist(3)}
    out, drop = _trim_chat_images(p, 3)
    assert drop == 0
    assert out is p


def test_f3_tiene_le_piu_recenti():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "primo"},
        {"type": "image_url", "image_url": {"url": "A"}},
        {"type": "image_url", "image_url": {"url": "B"}},
        {"type": "image_url", "image_url": {"url": "C"}}]}]
    p = {"messages": msgs}
    out, drop = _trim_chat_images(p, 2)
    assert drop == 1
    assert _kept_urls(out) == ["B", "C"]


def test_f3_turno_corrente_ha_priorita():
    """Se il turno corrente ha piu' immagini del tetto, si tiene la parte piu'
    recente di QUELLO, non del passato."""
    msgs = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "old1"}},
            {"type": "image_url", "image_url": {"url": "old2"}}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "cur1"}},
            {"type": "image_url", "image_url": {"url": "cur2"}}]},
    ]
    p = {"messages": msgs}
    out, drop = _trim_chat_images(p, 2)
    assert drop == 2
    assert _kept_urls(out) == ["cur1", "cur2"]


def test_f3_turno_corrente_parziale_completa_il_passato():
    """1 immagine nel turno corrente + tetto 3: le altre 2 dal passato."""
    msgs = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "a"}},
            {"type": "image_url", "image_url": {"url": "b"}},
            {"type": "image_url", "image_url": {"url": "c"}}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "cur"}}]},
    ]
    p = {"messages": msgs}
    out, drop = _trim_chat_images(p, 3)
    assert drop == 1
    assert _kept_urls(out) == ["b", "c", "cur"]


def test_f3_originale_intatto_copy_on_write():
    p = {"messages": _hist(4)}
    before = len(_kept_urls(p))
    out, drop = _trim_chat_images(p, 2)
    assert drop == 2
    assert len(_kept_urls(p)) == before     # payload del caller intatto
    assert out is not p


def test_f3_non_muta_contenti_non_immagine():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "t1"},
        {"type": "image_url", "image_url": {"url": "A"}},
        {"type": "text", "text": "t2"}]},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "B"}},
            {"type": "text", "text": "t3"}]}]
    p = {"messages": msgs}
    out, _ = _trim_chat_images(p, 1)
    texts = [pt["text"] for m in out["messages"]
             if isinstance(m.get("content"), list)
             for pt in m["content"] if pt.get("type") == "text"]
    assert texts == ["t1", "t2", "t3"]      # nessun testo perso


def test_f3_messaggi_stringa_intatti():
    p = {"messages": [{"role": "user", "content": "testo semplice"}]}
    out, drop = _trim_chat_images(p, 1)
    assert drop == 0 and out is p


def test_f3_knob_default_3_e_validazione():
    import app.policy as P
    pol = P.Policy()
    assert pol.chat_images_max == 3
    # il tetto e' dichiarato anche nello schema dei knob
    assert P.YAML_PATHS.get("chat_images_max") == \
        "capability_routing.chat_images_max"
    with pytest.raises(ValueError):
        P.Policy.from_dict({"capability_routing": {"chat_images_max": -1}})
    with pytest.raises(ValueError):
        P.Policy.from_dict({"capability_routing": {"chat_images_max": "x"}})
    assert P.Policy.from_dict(
        {"capability_routing": {"chat_images_max": 0}}).chat_images_max == 0
