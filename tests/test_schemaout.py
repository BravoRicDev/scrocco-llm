import json

from app.schemaout import (SchemaOutConfig, clean_json_content,
                           enforce_response, maybe_inject_response_format,
                           repair_by_schema, validate_schema)


def test_clean_from_fence():
    assert clean_json_content('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_clean_from_prose():
    assert clean_json_content('Ecco: {"a": 1} fatto') == '{"a": 1}'


def test_clean_invalid():
    assert clean_json_content("nessun json") is None


def test_validate_required():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"}}}
    assert validate_schema({"a": "x"}, schema) is None
    assert validate_schema({}, schema) is not None


def test_validate_type_and_extra():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}},
              "additionalProperties": False}
    assert validate_schema({"a": "x"}, schema) is not None
    assert validate_schema({"a": 1, "b": 2}, schema) is not None


def test_repair_by_schema():
    schema = {"type": "object", "required": ["n"],
              "properties": {"n": {"type": "integer"},
                             "s": {"type": "string"}}}
    obj, moves = repair_by_schema({"n": "5", "s": None}, schema)
    assert obj == {"n": 5} and "coerce_int" in moves and "drop_null" in moves


def _resp(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": content},
                         "finish_reason": "stop"}]}


def test_enforce_cleaned():
    data = _resp('```json\n{"ok": true}\n```')
    payload = {"response_format": {"type": "json_object"}}
    rep = enforce_response(data, payload, SchemaOutConfig())
    assert rep["status"] == "cleaned"
    assert data["choices"][0]["message"]["content"] == '{"ok": true}'


def test_enforce_schema_repaired():
    data = _resp('```json\n{"n": "7"}\n```')
    payload = {"response_format": {"type": "json_schema", "json_schema": {
        "schema": {"type": "object", "required": ["n"],
                   "properties": {"n": {"type": "integer"}}}}}}
    cfg = SchemaOutConfig(strict_schema=True, repair_content=True)
    rep = enforce_response(data, payload, cfg)
    assert rep["status"] in ("repaired", "cleaned")
    assert json.loads(data["choices"][0]["message"]["content"]) == {"n": 7}


def test_enforce_invalid():
    data = _resp("non e' json")
    payload = {"response_format": {"type": "json_object"}}
    rep = enforce_response(data, payload, SchemaOutConfig())
    assert rep["status"] == "invalid"


def test_notjson_plain():
    data = _resp("Ciao!")
    assert enforce_response(data, {}, SchemaOutConfig())["status"] == "notjson"


def test_inject_response_format():
    body = {}
    cfg = SchemaOutConfig(inject_response_format=True,
                          allow_providers=("openai",))
    assert maybe_inject_response_format(body, {"provider": "openai"}, cfg) is True
    assert body["response_format"] == {"type": "json_object"}
    body2 = {}
    assert maybe_inject_response_format(
        body2, {"provider": "groq"}, cfg) is False
