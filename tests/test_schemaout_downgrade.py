"""Degradazione gentile di response_format (json_schema) per provider non
nativi: rimozione del campo + istruzione schema nel prompt."""
from app.schemaout import (SchemaOutConfig, create_schemaout_config,
                           downgrade_response_format)

SCHEMA = {"type": "object",
          "properties": {"name": {"type": "string"}},
          "required": ["name"]}


def _payload(messages=None, rf=None):
    p = {"messages": messages or [{"role": "user", "content": "ciao"}]}
    if rf is not None:
        p["response_format"] = rf
    return p


def _dep(provider):
    return {"provider": provider, "api_base": f"https://{provider}.example/v1"}


def test_downgrade_non_native_provider():
    body = _payload(rf={"type": "json_schema", "json_schema": {"schema": SCHEMA}})
    rep = downgrade_response_format(body, _dep("groq"))
    assert rep and rep["kind"] == "json_schema"
    assert "response_format" not in body
    text = body["messages"][-1]["content"]
    assert "JSON" in text and "name" in text


def test_native_provider_untouched():
    body = _payload(rf={"type": "json_schema", "json_schema": {"schema": SCHEMA}})
    assert downgrade_response_format(body, _dep("openai")) is None
    assert "response_format" in body


def test_json_object_not_downgraded():
    body = _payload(rf={"type": "json_object"})
    assert downgrade_response_format(body, _dep("groq")) is None


def test_no_response_format_noop():
    assert downgrade_response_format(_payload(), _dep("groq")) is None


def test_instruction_goes_to_system_when_present():
    msgs = [{"role": "system", "content": "sei un bot"},
            {"role": "user", "content": "dammi json"}]
    body = _payload(messages=msgs,
                    rf={"type": "json_schema", "json_schema": {"schema": SCHEMA}})
    rep = downgrade_response_format(body, _dep("groq"))
    assert rep["where"] == "system"
    assert "json" in body["messages"][0]["content"].lower()


def test_original_messages_not_mutated():
    msgs = [{"role": "user", "content": "ciao"}]
    body = _payload(messages=msgs,
                    rf={"type": "json_schema", "json_schema": {"schema": SCHEMA}})
    downgrade_response_format(body, _dep("groq"))
    assert msgs[0]["content"] == "ciao"


def test_disabled_by_config():
    cfg = SchemaOutConfig(downgrade_response_format=False)
    body = _payload(rf={"type": "json_schema", "json_schema": {"schema": SCHEMA}})
    assert downgrade_response_format(body, _dep("groq"), cfg) is None


def test_create_config_from_policy_dict():
    cfg = create_schemaout_config({"qc_json": {
        "downgrade_response_format": False,
        "native_schema_providers": ["groq", "google"]}})
    assert cfg.downgrade_response_format is False
    assert cfg.native_schema_providers == ("groq", "google")
