from app.texttoolparse import (TextToolcallConfig, apply_to_message,
                               parse_text_toolcalls)


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
