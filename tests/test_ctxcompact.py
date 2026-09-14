"""Test per il troncamento cache-aware del contesto (app/ctxcompact.py)."""

from types import SimpleNamespace

from app.ctxcompact import (CtxCompactConfig, compact_tool_outputs,
                            create_ctxcompact_config,
                            ctxcompact_config_from_policy, should_compact)


def _tool(content):
    return {"role": "tool", "tool_call_id": "c", "content": content}


def _asst():
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}]}


def _conversation(t1=5000, t2=5000, t3=5000):
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        _asst(), _tool("x" * t1),
        {"role": "user", "content": "u2"},
        _asst(), _tool("y" * t2),
        {"role": "user", "content": "u3"},
        _asst(), _tool("z" * t3),
    ]


class TestConfig:
    def test_defaults(self):
        c = CtxCompactConfig()
        assert c.enabled is True
        assert c.keep_turns == 4
        assert c.min_saved_tokens == 500
        assert "{n}" in c.stub_text
        assert c.min_ctx_tokens == 50000
        assert c.on_deployment_switch is True
        assert c.switch_min_tokens == 8000

    def test_from_dict(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "enabled": False, "keep_turns": 2, "max_tool_output_chars": 50,
            "min_saved_tokens": 10, "stub_text": "[cut {n}]"}}})
        assert c.enabled is False
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 50
        assert c.min_saved_tokens == 10
        assert c.stub_text == "[cut {n}]"

    def test_from_dict_triggers(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "min_ctx_tokens": 1234, "on_deployment_switch": False,
            "switch_min_tokens": 99}}})
        assert c.min_ctx_tokens == 1234
        assert c.on_deployment_switch is False
        assert c.switch_min_tokens == 99

    def test_from_policy(self):
        pol = SimpleNamespace(cache_ctx_truncation_enabled=True,
                              cache_ctx_keep_turns=2,
                              cache_ctx_max_tool_output_chars=100,
                              cache_ctx_min_saved_tokens=5,
                              cache_ctx_stub_text="[s {n}]")
        c = ctxcompact_config_from_policy(pol)
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 100
        assert c.stub_text == "[s {n}]"
        # default dei nuovi inneschi
        assert c.min_ctx_tokens == 50000
        assert c.on_deployment_switch is True
        assert c.switch_min_tokens == 8000


class TestShouldCompact:
    def test_disabled(self):
        d = should_compact(CtxCompactConfig(enabled=False), 999999)
        assert d["compact"] is False

    def test_overflow(self):
        d = should_compact(CtxCompactConfig(), 150000, max_in=100000)
        assert d["compact"] is True
        assert "overflow" in d["reason"]

    def test_abs_threshold(self):
        # Con l'isteresi anti-churn la soglia assoluta scatta solo avvicinandosi
        # alla saturazione della finestra del deployment (qui 90% di 1M).
        d = should_compact(CtxCompactConfig(), 900000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is True
        assert "abs" in d["reason"]

    def test_abs_deferred_with_headroom(self):
        # cache calda + ampio margine: NON riscrivere il prefisso (no churn).
        d = should_compact(CtxCompactConfig(), 60000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is False

    def test_below_all_thresholds(self):
        d = should_compact(CtxCompactConfig(), 5000, max_in=1000000)
        assert d["compact"] is False

    def test_switch_cold(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder=None, dep_unique="d1")
        assert d["compact"] is True
        assert "switch" in d["reason"]
        assert d["cold"] is True

    def test_switch_holder_differs(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder="other", dep_unique="d1")
        assert d["compact"] is True and "switch" in d["reason"]

    def test_no_switch_when_hot(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is False
        assert d["cold"] is False

    def test_switch_below_min(self):
        d = should_compact(CtxCompactConfig(), 4000, max_in=1000000,
                           holder=None, dep_unique="d1")
        assert d["compact"] is False

    def test_switch_disabled(self):
        cfg = CtxCompactConfig(on_deployment_switch=False)
        d = should_compact(cfg, 10000, max_in=1000000, holder=None,
                           dep_unique="d1")
        assert d["compact"] is False

    def test_sticky(self):
        d = should_compact(CtxCompactConfig(), 1000, max_in=1000000,
                           session_compact=True)
        assert d["compact"] is True
        assert "sticky" in d["reason"]


class TestCompact:
    def test_stubs_only_old(self):
        msgs = _conversation()
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=2))
        assert rep["changed"] is True
        assert rep["stubbed"] == 1                 # solo il primo tool
        assert new[3]["content"].startswith("[tool output omesso")
        assert new[6]["content"] == "y" * 5000     # ultimi 2 turni intatti
        assert new[9]["content"] == "z" * 5000
        assert msgs[3]["content"] == "x" * 5000    # input non mutato

    def test_pairing_preserved(self):
        msgs = _conversation()
        new, _ = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=1))
        assert len(new) == len(msgs)
        assert [m["role"] for m in new] == [m["role"] for m in msgs]
        assert new[2].get("tool_calls") and new[3]["role"] == "tool"

    def test_idempotent(self):
        msgs = _conversation()
        cfg = CtxCompactConfig(keep_turns=2)
        once, rep1 = compact_tool_outputs(msgs, cfg)
        assert rep1["changed"] is True
        twice, rep2 = compact_tool_outputs(once, cfg)
        assert rep2["changed"] is False
        assert twice == once

    def test_small_output_untouched(self):
        msgs = _conversation(t1=50)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, max_tool_output_chars=2000))
        assert rep["changed"] is False

    def test_min_saved_gate(self):
        msgs = _conversation(t1=800)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, min_saved_tokens=100000))
        assert rep["changed"] is False
        assert new == msgs

    def test_no_user_message(self):
        msgs = [{"role": "system", "content": "s"}, _asst(), _tool("x" * 9000)]
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig())
        assert rep["changed"] is False


# ============================================================ v2: head+tail
def _conv2(calls):
    """[user] + N×(assistant[bash cid_i] + tool(content_i)) + [user] finale."""
    msgs = [{"role": "user", "content": "inizio"}]
    for j, content in enumerate(calls):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{j}", "type": "function",
                                     "function": {"name": "bash",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{j}",
                     "content": content})
    msgs.append({"role": "user", "content": "fine"})
    return msgs


class TestConfigV2:
    def test_defaults_v2(self):
        c = CtxCompactConfig()
        assert c.head_chars == 600 and c.tail_chars == 600
        assert c.keep_tail_pct == 2.0 and c.keep_error_outputs is True

    def test_from_dict_v2(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "head_chars": 300, "tail_chars": 0, "keep_tail_pct": 0.4,
            "keep_error_outputs": False}}})
        assert c.head_chars == 300 and c.tail_chars == 0
        assert c.keep_tail_pct == 0.4 and c.keep_error_outputs is False

    def test_from_policy_v2(self):
        pol = SimpleNamespace(cache_ctx_head_chars=100, cache_ctx_tail_chars=50,
                              cache_ctx_keep_tail_pct=1.5,
                              cache_ctx_keep_error_outputs=False)
        c = ctxcompact_config_from_policy(pol)
        assert c.head_chars == 100 and c.tail_chars == 50
        assert c.keep_tail_pct == 1.5 and c.keep_error_outputs is False


class TestRichStub:
    def test_head_tail_deterministic_and_line_safe(self):
        lines = [f"riga-{i:04d} " + "x" * 60 for i in range(200)]
        body = "\n".join(lines) + "\n"          # ~13200 char
        msgs = _conv2([body, "piccolo"])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        content = new[2]["content"]
        assert content.startswith("[tool output omesso:")   # riga 1 legacy
        assert "[bash]" in content.splitlines()[1]          # summary riga 2
        assert "righe" in content
        assert "...[omessi " in content
        head_part = content.split("\n", 2)[2].split("...[omessi")[0]
        assert head_part.startswith("riga-0000")
        assert head_part.endswith("\n")                     # mai a meta' riga
        assert content.split("...[omessi ")[-1].split("]...\n", 1)[1]
        tail_part = content.split("]...\n", 1)[1]
        assert tail_part.startswith("riga-") and "\nriga-" in tail_part
        # puro: stesso input -> stessi byte
        new2, _ = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        assert new2[2]["content"] == content
        assert rep["stubbed"] == 1

    def test_plain_stub_when_no_head_tail(self):
        msgs = _conv2(["y" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, head_chars=0, tail_chars=0)
        new, rep = compact_tool_outputs(msgs, cfg, )
        assert new[2]["content"] == "[tool output omesso: 9000 caratteri]"
        assert rep["changed"] is True

    def test_exit_zero_visible_and_stubbed(self):
        body = ("output\n" * 300) + "exit code: 0\n"
        msgs = _conv2([body])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["changed"] is True
        assert ", exit 0" in new[2]["content"]

    def test_errors_never_touched_even_overflow(self):
        corpo = ("Traceback (most recent call last):\n  File \"x.py\"\n"
                 + "z" * 9000)
        msgs = _conv2([corpo])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=0, min_saved_tokens=0))
        assert rep["changed"] is False
        assert new[2]["content"] == corpo
        # ValueError/ENOSPC riconosciuti allo stesso modo
        msgs2 = _conv2(["bla\nValueError: boom\n" + "w" * 9000])
        new2, _ = compact_tool_outputs(
            msgs2, CtxCompactConfig(keep_turns=1, min_saved_tokens=0))
        assert new2[2]["content"] == msgs2[2]["content"]

    def test_error_guard_disattivabile(self):
        corpo = "ValueError: boom\n" + "q" * 9000
        msgs = _conv2([corpo])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10,
                               keep_error_outputs=False)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["changed"] is True

    def test_multimodal_content_intatta(self):
        big = [{"type": "text", "text": "t" * 9000},
               {"type": "image_url", "image_url": {"url": "dataright"}}]
        msgs = _conv2([big])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        assert new[2]["content"] == big

    def test_custom_stub_text_prima_riga(self):
        msgs = _conv2(["l" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10,
                               stub_text="[cut {n}]")
        new, _ = compact_tool_outputs(msgs, cfg)
        assert new[2]["content"].startswith("[cut 9000]")
        assert "...[omessi " in new[2]["content"]          # head+tail attivo


class TestDedup:
    def test_rimando_al_doppione(self):
        corpo = "ripeti\n" * 2000                          # 14000 char
        msgs = _conv2([corpo, corpo])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["deduped"] == 1
        assert new[2]["content"].startswith("[rimando:")
        assert "c1" in new[2]["content"]                  # il piu' recente
        # il target (piu' vecchio di boundary? qui entrambi <boundary) viene
        # comunque stubbato: il rimando resta un puntatore valido
        assert new[4]["content"].startswith("[tool output omesso:")
        # idempotenza: il rimando non viene rielaborato
        new2, rep2 = compact_tool_outputs(new, cfg)
        assert rep2["changed"] is False

    def test_dedup_non_tocca_gli_errori(self):
        corpo = "Traceback x\n" + "e" * 9000
        msgs = _conv2([corpo, corpo])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=0))
        assert rep["deduped"] == 0 and rep["changed"] is False


class TestDynamicBoundary:
    def _three_big_turns(self, per_turn=6):
        """user + [per_turn x (asst+tool 4k char)] + user, ripetuto x3 turni."""
        body = "out\n" * 1000                              # 4000 char
        msgs = [{"role": "user", "content": "u0"}]
        k = 0
        for t in range(3):
            for _ in range(per_turn):
                msgs.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": f"c{k}", "type": "function",
                                             "function": {"name": "bash",
                                                          "arguments": "{}"}}]})
                msgs.append({"role": "tool", "tool_call_id": f"c{k}",
                             "content": body})
                k += 1
            msgs.append({"role": "user", "content": f"u{t + 1}"})
        return msgs

    def _stubbed_between(self, msgs, lo, hi):
        return sum(1 for i, m in enumerate(msgs)
                   if lo <= i < hi and m.get("role") == "tool"
                   and isinstance(m.get("content"), str)
                   and m["content"].startswith("[tool output omesso"))

    def test_budget_stringe_dentro_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        b_user = users[-2]                    # keep_turns=2 -> penultimo user
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=1.0)          # 1% di 400k = 4000tok
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] > b_user        # pota DENTRO i turni tenuti
        assert self._stubbed_between(new, b_user, rep["boundary"]) >= 1
        # la coda dentro budget resta integra
        for i in range(rep["boundary"], len(new)):
            if new[i].get("role") == "tool":
                assert new[i]["content"].startswith("out")

    def test_budget_generoso_non_prune_piu_di_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=50.0)         # 200k tok: tutto
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] == users[-2]    # max(B_user, 0) = B_user
        assert self._stubbed_between(new, 0, len(msgs)) >= 1

    def test_budget_disattivato_usa_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=0.0)
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] == users[-2]

    def test_floor_8_messaggi(self):
        msgs = self._three_big_turns(2)       # 16 msg: floor 8
        n = len(msgs)
        cfg = CtxCompactConfig(keep_turns=3, min_saved_tokens=0,
                               keep_tail_pct=0.001)        # budget ~0
        new, rep = compact_tool_outputs(msgs, cfg, max_in=100000)
        assert rep["boundary"] == n - 8        # floor: gli ultimi 8 integri
        for i in range(n - 8, n):
            if new[i].get("role") == "tool":
                assert new[i]["content"].startswith("out")


class TestEstimator:
    def test_saved_uses_estimator(self):
        msgs = _conv2(["m" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=50)
        new, rep = compact_tool_outputs(
            msgs, cfg, estimator=lambda ms: sum(len(str(m)) for m in ms) // 100)
        # //100 e' piu' severo del //4: il gate 50 puo' bloccare il //4-only?
        # qui 9000//100=90 > 50 -> passa, e il numero NON e' saved_chars//4
        assert rep["changed"] is True
        assert rep["saved_tokens_est"] != rep["saved_chars"] // 4

    def test_gate_estimator_severo(self):
        msgs = _conv2(["n" * 3000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=100)
        new, rep = compact_tool_outputs(
            msgs, cfg, estimator=lambda ms: sum(len(str(m)) for m in ms) // 1000)
        assert rep["changed"] is False
        assert new == msgs
