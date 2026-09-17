"""Bonifica `content` array -> stringa per provider a schema stretto (CF).

Sotto-firma del 400 Cloudflare Workers AI "Bad input ... 'array' not in
'string' / required properties at '/messages/N' are 'role,content'".
La bonifica deve essere LOSSLESS e MEDIA-SAFE: appiattisce solo gli array di
solo testo e aggiunge `content:""` agli assistant con tool_calls; se compare
un blocco media il messaggio resta intatto (si lascia la rotazione).
"""
from app.forwarder import apply_content_string
from app.histnorm import flatten_text_content


def _txt(s):
    return {"type": "text", "text": s}


def test_array_di_testo_diventa_stringa():
    msgs = [{"role": "user",
             "content": [_txt("hi "), _txt("there")]}]
    out, n = flatten_text_content(msgs)
    assert n == 1
    assert out[0]["content"] == "hi there"
    assert out[0]["role"] == "user"


def test_input_text_equivalente():
    msgs = [{"role": "system",
             "content": [{"type": "input_text", "text": "sys"}]}]
    out, n = flatten_text_content(msgs)
    assert n == 1 and out[0]["content"] == "sys"


def test_parte_senza_type_ma_con_text():
    msgs = [{"role": "user", "content": [{"text": "a"}, {"text": "b"}]}]
    out, n = flatten_text_content(msgs)
    assert n == 1 and out[0]["content"] == "ab"


def test_media_preservato():
    media = [{"type": "text", "text": "guarda"},
             {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
    msgs = [{"role": "user", "content": media}]
    out, n = flatten_text_content(msgs)
    assert n == 0
    assert out[0]["content"] is media          # intatto, stessa lista


def test_parte_ignota_preservata():
    msgs = [{"role": "user",
             "content": [_txt("x"), {"type": "weird", "text": "y"}]}]
    out, n = flatten_text_content(msgs)
    assert n == 0 and out[0]["content"] == msgs[0]["content"]


def test_assistant_tool_calls_senza_content():
    msgs = [{"role": "assistant",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "x", "arguments": "{}"}}]}]
    out, n = flatten_text_content(msgs)
    assert n == 1 and out[0]["content"] == ""


def test_assistant_senza_tool_calls_non_toccato():
    msgs = [{"role": "assistant"}]
    out, n = flatten_text_content(msgs)
    assert n == 0 and "content" not in out[0]


def test_stringa_invariata():
    msgs = [{"role": "user", "content": "gia' stringa"}]
    out, n = flatten_text_content(msgs)
    assert n == 0 and out[0] is msgs[0]


def test_lista_vuota_diventa_stringa_vuota():
    msgs = [{"role": "user", "content": []}]
    out, n = flatten_text_content(msgs)
    assert n == 1 and out[0]["content"] == ""


def test_input_mai_mutato():
    msgs = [{"role": "user", "content": [_txt("x")]},
            {"role": "assistant", "tool_calls": [{"id": "c"}]}]
    import copy
    snapshot = copy.deepcopy(msgs)
    flatten_text_content(msgs)
    assert msgs == snapshot                     # nessuna mutazione in-place


def test_idempotente():
    msgs = [{"role": "user", "content": [_txt("x")]},
            {"role": "assistant", "tool_calls": [{"id": "c"}]}]
    out, _ = flatten_text_content(msgs)
    out2, n2 = flatten_text_content(out)
    assert n2 == 0 and out2 == out


def test_apply_content_string_gated_sul_flag():
    body = {"messages": [{"role": "user", "content": [_txt("q")]}]}
    assert apply_content_string(body, {"provider": "cloudflare"}) == 0
    assert body["messages"][0]["content"] == [_txt("q")]      # non flaggato
    assert apply_content_string(body, {"content_string": True}) == 1
    assert body["messages"][0]["content"] == "q"
