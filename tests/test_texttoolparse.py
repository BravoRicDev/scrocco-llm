from app.texttoolparse import (TextToolcallConfig, apply_to_message,
                               parse_text_toolcalls, strip_toolid_markup)


def _tools(*names):
    return [{"type": "function", "function": {"name": n, "parameters": {}}}
            for n in names]


def test_function_xml():
    content = ("<function=read><parameter=filePath>/x.txt</parameter>"
               "<parameter=limit>10</parameter></function>")
    calls = parse_text_toolcalls(content, _tools("read"))
    assert calls and calls[0]["function"]["name"] == "read"
    import json
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"filePath": "/x.txt", "limit": 10}


def test_require_declared_name_rejects():
    content = "<function=delete><parameter=path>/x</parameter></function>"
    cfg = TextToolcallConfig(require_declared_name=True)
    assert parse_text_toolcalls(content, _tools("read"), cfg) is None
    cfg2 = TextToolcallConfig(require_declared_name=False)
    assert parse_text_toolcalls(content, _tools("read"), cfg2) is not None


def test_antml():
    content = ('antml:invoke name="bash"'
               '<parameter name="command">ls -la</parameter></antml:invoke>')
    calls = parse_text_toolcalls(content, _tools("bash"))
    assert calls and calls[0]["function"]["name"] == "bash"


def test_tool_call_json():
    content = '<tool_call>{"name": "grep", "arguments": {"pattern": "x"}}</tool_call>'
    calls = parse_text_toolcalls(content, _tools("grep"))
    assert calls and calls[0]["function"]["name"] == "grep"


def test_argkv():
    content = ("<function=edit><arg_key>filePath</arg_key>"
               "<arg_value>/a</arg_value><arg_key>oldString</arg_key>"
               "<arg_value>x</arg_value></function>")
    calls = parse_text_toolcalls(content, _tools("edit"))
    assert calls and calls[0]["function"]["name"] == "edit"


def test_bare_json():
    content = '```json\n{"name": "bash", "arguments": {"command": "pwd"}}\n```'
    calls = parse_text_toolcalls(content, _tools("bash"))
    assert calls and calls[0]["function"]["name"] == "bash"


def test_prose_returns_none():
    assert parse_text_toolcalls("Ciao, come posso aiutarti?", _tools("bash")) is None


def test_apply_to_message():
    msg = {"role": "assistant",
           "content": "<function=bash><parameter=command>pwd</parameter></function>"}
    info = apply_to_message(msg, _tools("bash"), TextToolcallConfig())
    assert info and msg["content"] == "" and msg["tool_calls"]
    assert msg["tool_calls"][0]["function"]["name"] == "bash"


# Campione reale (301 char) emesso come TESTO da ling-3.0-flash-fin-free.
TOOLID_SAMPLE = (
    '<goal status="completed"/>\n'
    '<goal_status status="completed"/>\n'
    '<tool_call_id>toolu_bdrk_01S2UB8W3Jg3DkP3R1jF3e8M</tool_call_id>\n'
    '<tool_call_type>bash</tool_call_type>\n'
    '<tool_input>{"command":"node scripts/test-impersonation.mjs 2>&1",'
    '"workdir":"/home/lumon/Serverino/crm-v2","timeout":120000}</tool_input>')


def test_toolid_triple_reconstructed():
    import json
    calls = parse_text_toolcalls(TOOLID_SAMPLE, _tools("bash"))
    assert calls and len(calls) == 1
    c = calls[0]
    assert c["function"]["name"] == "bash"
    assert c["id"] == "toolu_bdrk_01S2UB8W3Jg3DkP3R1jF3e8M"
    assert json.loads(c["function"]["arguments"]) == {
        "command": "node scripts/test-impersonation.mjs 2>&1",
        "workdir": "/home/lumon/Serverino/crm-v2", "timeout": 120000}


def test_toolid_undeclared_returns_none():
    assert parse_text_toolcalls(TOOLID_SAMPLE, _tools("read")) is None


def test_toolid_bad_json_returns_none():
    bad = ("<tool_call_type>bash</tool_call_type>"
           "<tool_input>non json</tool_input>")
    assert parse_text_toolcalls(bad, _tools("bash")) is None


def test_strip_toolid_markup_keeps_goal():
    out = strip_toolid_markup(TOOLID_SAMPLE)
    assert "tool_call_type" not in out and "tool_input" not in out
    assert '<goal status="completed"/>' in out
    assert '<goal_status status="completed"/>' in out


def test_apply_to_message_preserve_residual():
    msg = {"role": "assistant", "content": TOOLID_SAMPLE}
    info = apply_to_message(msg, _tools("bash"), TextToolcallConfig(),
                            preserve_residual=True)
    assert info and msg["tool_calls"]
    assert msg["tool_calls"][0]["function"]["name"] == "bash"
    assert "tool_call_type" not in msg["content"]
    assert "<goal" in msg["content"]          # residuo conservato
