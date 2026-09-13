"""Feature: riparazione argomenti tool-call (tool_repair).

Covers:
- unit toolrepair: ogni violazione -> output valido; input valido -> invariato;
  livello off -> nessun intervento
- unit config/csv_store: parsing flag, default, off, stabilita drow_*
- integrazione non-streaming: nessuna rotazione quando riparato
- integrazione streaming: frammentati, multipli, troncato
- non-regressione: richieste senza tools e contenuti utente non toccati
"""
import asyncio
import json

import pytest

from app.toolrepair import (
    ToolRepairConfig,
    ToolRepairSSEFilter,
    repair_arguments,
    repair_tool_calls,
    parse_tool_repair_value,
    resolve_level,
    create_tool_repair_config,
)


# --------------------------------------------------- helpers

def _dep(tool_repair="", base="https://openrouter.ai/api/v1"):
    return {"api_base": base, "tool_repair": tool_repair, "model": "m",
            "provider": "p", "api_key": "k", "unique": "test-dep"}


def _dep_google(tool_repair=""):
    return {"api_base": "https://generativelanguage.googleapis.com",
            "tool_repair": tool_repair, "model": "gemini-3.5-flash",
            "provider": "google", "api_key": "k", "unique": "google-dep"}


def _msg(tool_calls):
    return {"choices": [{"message": {"tool_calls": tool_calls},
                         "finish_reason": "stop"}]}


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": args}}


def _payload_with_tools():
    return {"tools": [{"type": "function", "function": {
        "name": "search", "parameters": {"type": "object",
        "properties": {"q": {"type": "string"}}}}}]}


# --------------------------------------------------- parse / resolve

class TestParseToolRepairValue:
    def test_empty_default_aggressive(self):
        assert parse_tool_repair_value("") == "aggressive"
        assert parse_tool_repair_value(None) == "aggressive"
        assert parse_tool_repair_value("  ") == "aggressive"

    def test_valid_values(self):
        assert parse_tool_repair_value("off") == "off"
        assert parse_tool_repair_value("safe") == "safe"
        assert parse_tool_repair_value("aggressive") == "aggressive"
        assert parse_tool_repair_value("OFF") == "off"
        assert parse_tool_repair_value("Safe") == "safe"

    def test_invalid_fallback_aggressive(self):
        assert parse_tool_repair_value("banana") == "aggressive"


class TestResolveLevel:
    def test_off_always_off(self):
        cfg = ToolRepairConfig(enabled=True)
        assert resolve_level(_dep("off"), cfg) == "off"

    def test_explicit_safe(self):
        cfg = ToolRepairConfig(enabled=True)
        assert resolve_level(_dep("safe"), cfg) == "safe"

    def test_empty_default_aggressive(self):
        cfg = ToolRepairConfig(enabled=True)
        assert resolve_level(_dep(""), cfg) == "aggressive"

    def test_disabled_policy(self):
        cfg = ToolRepairConfig(enabled=False)
        assert resolve_level(_dep("aggressive"), cfg) == "off"

    def test_google_auto_off(self):
        cfg = ToolRepairConfig(enabled=True, disable_for_google=True)
        assert resolve_level(_dep_google(""), cfg) == "off"

    def test_google_explicit_overrides_auto_off(self):
        cfg = ToolRepairConfig(enabled=True, disable_for_google=True)
        assert resolve_level(_dep_google("safe"), cfg) == "safe"
        assert resolve_level(_dep_google("aggressive"), cfg) == "aggressive"


# --------------------------------------------------- repair_arguments (safe)

class TestReescapeControlChars:
    def test_newline_unescaped_in_string(self):
        # JSON with unescaped newline (invalid JSON) -> re-escaped
        args = '{"q": "hello\nworld"}'  # raw newline, not \n escape
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello\nworld"}

    def test_valid_json_unchanged(self):
        args = '{"q": "hello"}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert not changed
        assert json.loads(result) == {"q": "hello"}


class TestExtractMarkdownFence:
    def test_json_in_fence(self):
        args = '```json\n{"q": "hello"}\n```'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}

    def test_no_fence_unchanged(self):
        args = '{"q": "hello"}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert not changed


class TestTrailingComma:
    def test_comma_before_brace(self):
        args = '{"q": "hello",}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}

    def test_comma_before_bracket(self):
        args = '[1,2,]'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == [1, 2]


class TestPythonStyleBools:
    def test_true_false(self):
        args = '{"flag": True, "x": False}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        parsed = json.loads(result)
        assert parsed["flag"] is True
        assert parsed["x"] is False


class TestEmptyStringToObject:
    def test_empty_string(self):
        result, changed, moves = repair_arguments("", "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {}

    def test_empty_json_string(self):
        result, changed, moves = repair_arguments('""', "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {}


class TestCoerceStringifiedScalars:
    def test_string_number(self):
        args = '{"count": "42"}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"count": 42}

    def test_string_float(self):
        args = '{"ratio": "3.14"}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"ratio": 3.14}

    def test_string_bool(self):
        args = '{"flag": "true"}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"flag": True}


# --------------------------------------------------- repair_arguments (aggressive)

class TestCloseTruncatedJson:
    def test_missing_brace(self):
        args = '{"q": "hello"'
        result, changed, moves = repair_arguments(args, "aggressive", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}

    def test_missing_bracket(self):
        args = '{"list": [1, 2'
        result, changed, moves = repair_arguments(args, "aggressive", ToolRepairConfig())
        assert changed
        parsed = json.loads(result)
        assert "list" in parsed


class TestMixedQuoting:
    def test_single_quotes_to_double(self):
        args = "{'q': 'hello'}"
        result, changed, moves = repair_arguments(args, "aggressive", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}


class TestCollapseDoubleSerialization:
    def test_double_stringified_json(self):
        args = '"{\\"q\\": \\"hello\\"}"'
        result, changed, moves = repair_arguments(args, "aggressive", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}

    def test_array_wrapping_single_element(self):
        args = '["{\\"q\\": \\"hello\\"}"]'
        result, changed, moves = repair_arguments(args, "aggressive", ToolRepairConfig())
        assert changed
        assert json.loads(result) == {"q": "hello"}


# --------------------------------------------------- off level

class TestOffLevel:
    def test_off_no_intervention(self):
        args = '{"invalid json}'
        result, changed, moves = repair_arguments(args, "off", ToolRepairConfig())
        assert not changed
        assert result == args
        assert moves == []


# --------------------------------------------------- idempotency

class TestIdempotency:
    def test_valid_json_unchanged(self):
        args = '{"q": "hello world", "count": 42, "flag": true}'
        result, changed, moves = repair_arguments(args, "safe", ToolRepairConfig())
        assert not changed
        assert result == args

    def test_double_repair_noop(self):
        args = '```json\n{"q": "hello"}\n```'
        result1, _, _ = repair_arguments(args, "aggressive", ToolRepairConfig())
        result2, changed2, _ = repair_arguments(result1, "aggressive", ToolRepairConfig())
        assert not changed2  # idempotent on second pass


# --------------------------------------------------- repair_tool_calls (entry point)

class TestRepairToolCalls:
    def test_repaired_tool_calls(self):
        data = _msg([_tc("search", '{"q": "hello",}')])
        payload = _payload_with_tools()
        dep = _dep()
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is True
        assert "remove_trailing_comma" in result["moves"]
        # Il JSON deve essere valido dopo la riparazione
        tc_args = data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(tc_args) == {"q": "hello"}

    def test_valid_no_repair(self):
        data = _msg([_tc("search", '{"q": "hello"}')])
        payload = _payload_with_tools()
        dep = _dep()
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False

    def test_no_tools_no_repair(self):
        data = _msg([])
        payload = {}
        dep = _dep()
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False

    def test_off_level_no_repair(self):
        data = _msg([_tc("search", '{"q": "hello",}')])
        payload = _payload_with_tools()
        dep = _dep(tool_repair="off")
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False

    def test_google_auto_off(self):
        data = _msg([_tc("search", '{"q": "hello",}')])
        payload = _payload_with_tools()
        dep = _dep_google()
        cfg = ToolRepairConfig(disable_for_google=True)
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False

    def test_google_explicit_safe_not_auto_off(self):
        data = _msg([_tc("search", '{"q": "hello",}')])
        payload = _payload_with_tools()
        dep = _dep_google(tool_repair="safe")
        cfg = ToolRepairConfig(disable_for_google=True)
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is True


# --------------------------------------------------- streaming filter

class TestToolRepairSSEFilter:
    def test_tool_call_single_chunk(self):
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)

        # Chunk con tool call completo in un solo chunk
        chunk_data = {
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "hello",}'}
                }]},
                "finish_reason": None,
            }],
        }
        chunk_bytes = f"data: {json.dumps(chunk_data)}\n\n".encode()

        # Il chunk viene bufferizzato, non emesso subito
        out = filt.feed(chunk_bytes)
        assert out == []  # bufferizzato

        # Finish reasoning trigger flush
        done_chunk = {"choices": [{"finish_reason": "stop", "index": 0}]}
        done_bytes = f"data: {json.dumps(done_chunk)}\n\n".encode()
        out = filt.feed(done_bytes)

        # Dovrebbe emettere il tool call riparato + il done chunk
        assert len(out) >= 1
        # Verifica che il tool call riparato sia presente
        found_repaired = False
        for o in out:
            if b'"arguments"' in o:
                parsed = json.loads(o.decode().replace("data: ", "").strip())
                tc_args = parsed["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
                assert json.loads(tc_args) == {"q": "hello"}
                found_repaired = True
        assert found_repaired

    def test_off_level_passthrough(self):
        cfg = ToolRepairConfig()
        dep = _dep(tool_repair="off")
        filt = ToolRepairSSEFilter(cfg, dep)
        chunk = b"data: {\"choices\": []}\n\n"
        out = filt.feed(chunk)
        assert out == [chunk]

    def test_tools_absent_passthrough(self):
        """Se payload non ha tools, il filtro non si attiva."""
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)
        # Anche con level != off, senza payload.tools non si attiva
        chunk = b"data: {\"choices\": []}\n\n"
        out = filt.feed(chunk)
        assert out == [chunk]

    def test_multiple_tool_calls(self):
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)

        # Primo tool call
        chunk1 = {
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "search", "arguments": '{"q": "hello",}'}},
                    {"index": 1, "id": "call_2", "type": "function",
                     "function": {"name": "search2", "arguments": '{"q": "world"}'}},
                ]},
                "finish_reason": None,
            }],
        }
        filt.feed(f"data: {json.dumps(chunk1)}\n\n".encode())

        done = {"choices": [{"finish_reason": "stop", "index": 0}]}
        out = filt.feed(f"data: {json.dumps(done)}\n\n".encode())

        # Dovrebbe emettere 2 tool calls riparati + done
        assert len(out) >= 2

    def test_stats(self):
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)
        stats = filt.stats
        assert stats["repaired_count"] == 0
        assert stats["level"] == "aggressive"


# --------------------------------------------------- create_tool_repair_config

class TestCreateToolRepairConfig:
    def test_default(self):
        cfg = create_tool_repair_config(None)
        assert cfg.enabled is True
        assert cfg.default_level == "aggressive"
        assert cfg.disable_for_google is True
        assert cfg.max_args_size == 100000

    def test_from_dict(self):
        cfg = create_tool_repair_config({
            "tool_repair": {
                "enabled": False,
                "default_level": "safe",
                "disable_for_google": False,
                "max_args_size": 50000,
            }
        })
        assert cfg.enabled is False
        assert cfg.default_level == "safe"
        assert cfg.disable_for_google is False
        assert cfg.max_args_size == 50000

    def test_invalid_level_fallback(self):
        cfg = create_tool_repair_config({
            "tool_repair": {"default_level": "banana"}
        })
        assert cfg.default_level == "aggressive"


# --------------------------------------------------- config.py integration

class TestClassifyToolRepair:
    def test_empty_default_aggressive(self):
        from app.config import _classify
        from datetime import date
        row = {"modello": "test", "provider": "p", "endpoint": "https://x",
               "data": "free", "context": "128", "max_input": "1000",
               "priority": "0", "caps": "text", "tool_repair": ""}
        result = _classify(row, date.today())
        assert result["tool_repair"] == ""

    def test_safe_value(self):
        from app.config import _classify
        from datetime import date
        row = {"modello": "test", "provider": "p", "endpoint": "https://x",
               "data": "free", "context": "128", "max_input": "1000",
               "priority": "0", "caps": "text", "tool_repair": "safe"}
        result = _classify(row, date.today())
        assert result["tool_repair"] == "safe"

    def test_off_value(self):
        from app.config import _classify
        from datetime import date
        row = {"modello": "test", "provider": "p", "endpoint": "https://x",
               "data": "free", "context": "128", "max_input": "1000",
               "priority": "0", "caps": "text", "tool_repair": "off"}
        result = _classify(row, date.today())
        assert result["tool_repair"] == "off"

    def test_missing_column(self):
        from app.config import _classify
        from datetime import date
        row = {"modello": "test", "provider": "p", "endpoint": "https://x",
               "data": "free", "context": "128", "max_input": "1000",
               "priority": "0", "caps": "text"}
        result = _classify(row, date.today())
        assert result["tool_repair"] == ""


# --------------------------------------------------- regression: no tools

class TestNoToolsRegression:
    def test_content_not_touched(self):
        """Richieste senza tools: contenuto non toccato."""
        data = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        payload = {}  # no tools
        dep = _dep()
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False
        assert data["choices"][0]["message"]["content"] == "hello"

    def test_content_with_tools_not_touched(self):
        """Richieste con tools ma risposta senza tool_calls: contenuto non toccato."""
        data = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        payload = _payload_with_tools()
        dep = _dep()
        cfg = ToolRepairConfig()
        result = repair_tool_calls(data, payload, dep, cfg)
        assert result["repaired"] is False
        assert data["choices"][0]["message"]["content"] == "hello"


# --------------------------------------------------- streaming index/DONE fixes

class TestStreamingIndexAndDone:
    """Test per fix: indice corretto in tool_calls multipli + DONE finale in finalize()."""

    def test_multiple_tool_calls_correct_indices(self):
        """Due tool_calls con index 0 e 1 devono uscire con gli indici corretti."""
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)

        # Due tool_calls nello stesso chunk, index 0 e 1, entrambi con JSON malformato
        chunk1 = {
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "search", "arguments": '{"q": "hello",}'}},
                    {"index": 1, "id": "call_2", "type": "function",
                     "function": {"name": "search2", "arguments": '{"q": "world",}'}},
                ]},
                "finish_reason": None,
            }],
        }
        filt.feed(f"data: {json.dumps(chunk1)}\n\n".encode())

        # Trigger flush con finish_reason
        done = {"choices": [{"finish_reason": "stop", "index": 0}]}
        out = filt.feed(f"data: {json.dumps(done)}\n\n".encode())

        # Verifica che siano emessi DUE tool_calls con index corretto
        tc_indices = []
        for o in out:
            if b'"tool_calls"' in o:
                parsed = json.loads(o.decode().replace("data: ", "").strip())
                for tc in parsed["choices"][0]["delta"]["tool_calls"]:
                    tc_indices.append(tc["index"])

        assert sorted(tc_indices) == [0, 1], f"Indici tool_call: {tc_indices}"

    def test_finalize_emits_done_if_missing(self):
        """finalize() deve emettere [DONE] se il modello non l'ha mandato."""
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)

        # Feed un tool_call malformato, NO finish_reason, NO [DONE]
        chunk = {
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "hello",}'}
                }]},
                "finish_reason": None,
            }],
        }
        filt.feed(f"data: {json.dumps(chunk)}\n\n".encode())

        # Chiamiamo finalize() senza aver mandato [DONE] né finish_reason
        out = filt.finalize()

        # Deve aver flushato il tool_call riparato E aggiunto [DONE]
        found_done = any(b"[DONE]" in o for o in out)
        found_tc = any(b'"tool_calls"' in o for o in out)

        assert found_tc, "Tool call riparato non emesso"
        assert found_done, "[DONE] finale non emesso da finalize()"
        assert filt._done is True, "Flag _done non settato"

    def test_multiple_tool_calls_different_indices(self):
        """Tool_calls con index non sequenziali (es. 2 e 5) mantengono i loro indici."""
        cfg = ToolRepairConfig()
        dep = _dep()
        filt = ToolRepairSSEFilter(cfg, dep)

        chunk = {
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 2, "id": "call_a", "type": "function",
                     "function": {"name": "a", "arguments": '{"x":1,}'}},
                    {"index": 5, "id": "call_b", "type": "function",
                     "function": {"name": "b", "arguments": '{"y":2,}'}},
                ]},
                "finish_reason": None,
            }],
        }
        filt.feed(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {"choices": [{"finish_reason": "stop", "index": 0}]}
        out = filt.feed(f"data: {json.dumps(done)}\n\n".encode())

        tc_indices = []
        for o in out:
            if b'"tool_calls"' in o:
                parsed = json.loads(o.decode().replace("data: ", "").strip())
                for tc in parsed["choices"][0]["delta"]["tool_calls"]:
                    tc_indices.append(tc["index"])

        assert set(tc_indices) == {2, 5}, f"Indici tool_call: {tc_indices}"


# --------------------------------------------------- end of file
